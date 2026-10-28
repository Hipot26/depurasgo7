"""
/nuevo — Capa de integración SIPWEBORA ↔ SGS
=============================================
Une la lógica de balance de /pruebas (descarga SIPWEBORA) con la de sgs_api
(SGSClient / generar_balance) sin modificar ninguno de los dos módulos.

Excluye deliberadamente:
  · La subida manual de Excel nodo a nodo (procesar_sgs.py de /pruebas)
  · La validación de territorio / categorización de facturación
  · Todo write sobre SGS (solo lectura)

Cruce SIP ↔ SGS — lógica de matching de códigos
-------------------------------------------------
SGS expone productos como [DOM-028733-12/274287] DESCRIPCION...
  · DOM-028733-12  → código interno Odoo (NO existe en SIP)
  · 274287         → customer_default_code → código SIP equivalente

Estrategia de búsqueda por SIP_code:
  1. customer_default_code  (campo directo en project.material)
  2. cod_alt = parte después de "/" en el nombre del producto
  3. DOM code completo (fallback)
  4. Si nada casa, intenta SIP_code + "C" (regla chatarra-retorno)

Regla sufijo "C":
  Un código SGS terminado en "C" (ej. 251359C, descripción "(CUSTODIA)") es
  material que ENEL NO entregó y Dominion COMPRÓ (después se le cobra).
  Almacén lo despacha con la C; SIPWEBORA siempre trae el código pelado
  (251359). Es el MISMO material físico: 251359 y 251359C se cruzan como uno
  solo (neto SGS = suma de ambas variantes) y la fila se marca "(C)" para que
  facturación sepa que va cobrado aparte. NO es chatarra.

Colores en CRUCE_FACTURAS (vista facturación):
  · Amarillo  — chatarra SIP (tipo I RZ / R CH / R RZ) confirmada (no existe en SGS)
  · Azul (C)  — código SGS con sufijo C: material comprado por Dominion
  · Verde     — SIP qty == SGS neto (cuadra)
  · Rojo      — SIP qty != SGS neto (descuadre)
  · Ámbar     — solo en un sistema (sin contrapartida)

Colores en CRUCE_LIQUIDADOR (vista liquidador):
  · Excluye filas chatarra SIP
  · Normaliza sufijo C
  · Verde     — SIP qty == SGS neto
  · Rojo      — descuadre
  · Ámbar     — sin contrapartida

Uso como librería:
  from nuevo.integracion import BalanceIntegrado, cfg_desde_archivo
  cliente = BalanceIntegrado(**cfg_desde_archivo())
  resultado = cliente.ejecutar("17352", proyecto_id_sgs="49879")
  with open("balance.xlsx", "wb") as f:
      f.write(resultado["excel_bytes"])
"""

import io
import os
import re
import sys
from datetime import datetime

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

# ── Módulos vendored en nuevo/ (balance_liquidar, sgs_client, generar_balance) ──
_HERE        = os.path.dirname(os.path.abspath(__file__))
_SGS_API_DIR = os.path.join(os.path.dirname(_HERE), "sgs_api")  # config_sgs.json en raíz/sgs_api/

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import unicodedata

from balance_liquidar import (
    descargar_html, extraer_datos, _normalizar_nodo_sip, _nodo_endpoint_key,
)
from sgs_client import SGSClient, parsear_producto
from generar_balance import _agg_bandejas

# ── Paleta de colores ──────────────────────────────────────────────────────────
C_TITULO     = "1F3864"   # azul oscuro — títulos
C_HDR_SIP    = "2E75B6"   # azul        — cabecera SIPWEBORA
C_HDR_SGS    = "375623"   # verde oscuro — cabecera SGS
C_HDR_FACT   = "7030A0"   # morado      — cabecera vista facturadoras
C_HDR_SERG   = "833C00"   # marrón      — cabecera vista liquidador
C_SUBHDR     = "D6E4F0"   # azul claro  — subheader columnas
C_MAT_SUB    = "E2EFDA"   # verde claro — subheader materiales
C_PAR        = "F2F2F2"   # gris        — fila alternada
C_TOTAL      = "FFE699"   # amarillo    — filas de totales

# Colores de estado para cruces
C_VERDE      = "00B050"   # verde fuerte — cuadra exacto
C_ROJO       = "FF0000"   # rojo         — descuadre
C_AMARILLO   = "FFC000"   # amarillo     — chatarra (facturadoras)
C_AMBAR      = "FFEB9C"   # ámbar claro  — solo en un sistema
C_VERDE_SGS  = "C6EFCE"   # verde claro  — OK en SGS_BALANCE
C_ROJO_SGS   = "FFC7CE"   # rojo claro   — error en SGS_BALANCE
C_AZUL_C     = "DDEBF7"   # azul claro   — sufijo C: material comprado por Dominion (custodia)
C_ADVERT     = "FFC000"   # amarillo     — advertencia de consistencia

BLANCO       = "FFFFFF"

# Tipos SIP que identifican chatarra
CHATARRA_TIPOS = {"I RZ", "R CH", "R RZ"}

# Color para posible error de clasificación SIP
C_ERROR_CLASIF = "FF6600"   # naranja — chatarra en SIP pero existe en SGS → error probable

# tramo SIP sin nodo SGS del mismo nombre pero con un nodo SGS de los mismos materiales
C_EQUIV      = "B4A7D6"   # morado claro — equivalencia por materiales, pendiente de aprobar


# ── Equivalencias de nodo: mismo tramo, otro nombre en SIP y en SGS ──────
# Caso típico (22 oct 2026): la cuadrilla carga en SIP «22AL28», «17AL22» y «39AL41» y el supervisor
# crea en SGS N26_N28, N18_N22 y N39-N53, con las mismas cantidades. Si el cruce solo compara por nombre,
# el cable sale de almacén y nunca se liquida, y el Liquidador crea nodos gemelos para la MO. El cruce
# propone la pareja y la persona de facturación decide; lo aprobado queda guardado por LCL y se aplica en
# toda consulta siguiente.
_EQUIV_PATH = os.environ.get("BALANCE_EQUIV_PATH") or os.path.join(_HERE, "data", "equivalencias.json")


def equiv_clave_caso(padre: dict) -> str:
    """LCL (o DF) del caso; si no hay, el PRO del primer padre."""
    padre = padre or {}
    for t in str(padre.get("order_number") or "").split("+"):
        if _es_codigo_caso_real(t.strip()):
            return _base_codigo_caso(t.strip())
    return str(padre.get("code") or "").split("+")[0].strip()


def equiv_cargar() -> dict:
    try:
        import json
        with open(_EQUIV_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def equiv_decidir(clave_caso: str, eq: dict, decision: str, usuario: str = "") -> dict:
    """Guarda la decisión de la facturadora (aprobada / rechazada / pendiente = deshacer)."""
    import json
    import fcntl
    if not clave_caso or decision not in ("aprobada", "rechazada", "pendiente"):
        raise ValueError("decisión inválida")
    os.makedirs(os.path.dirname(_EQUIV_PATH), exist_ok=True)
    with open(_EQUIV_PATH + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        d = equiv_cargar()
        caso = d.setdefault(clave_caso, {})
        if decision == "pendiente":
            caso.pop(eq["sip"], None)
        else:
            caso[eq["sip"]] = {
                "sgs": eq["sgs"], "pro": eq.get("pro", ""), "estado": decision,
                "por": usuario or "", "en": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "materiales": eq.get("materiales") or [],
            }
        tmp = _EQUIV_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _EQUIV_PATH)
    return caso.get(eq["sip"], {})


# ── Helpers de celda ──────────────────────────────────────────────────────────

def _s():
    return Side(style="thin")


def _c(ws, row, col, value="", bold=False, bg=None, fg="000000",
       align_h="left", size=10, wrap=False, border=True,
       num_fmt=None, merge_to=None):
    cell = ws.cell(row=row, column=col, value=value)
    cell.font      = Font(bold=bold, color=fg, size=size, name="Calibri")
    cell.alignment = Alignment(horizontal=align_h, vertical="center", wrap_text=wrap)
    if bg:
        cell.fill = PatternFill("solid", fgColor=bg)
    if border:
        b = _s()
        cell.border = Border(left=b, right=b, top=b, bottom=b)
    if num_fmt:
        cell.number_format = num_fmt
    if merge_to:
        ws.merge_cells(
            start_row=row, start_column=col,
            end_row=row,   end_column=merge_to,
        )
    return cell


def _titulo_hoja(ws, row, text, color, ncols):
    ws.merge_cells(
        start_row=row, start_column=1,
        end_row=row,   end_column=ncols,
    )
    c = ws.cell(row=row, column=1, value=text)
    c.font      = Font(bold=True, color=BLANCO, size=13, name="Calibri")
    c.fill      = PatternFill("solid", fgColor=color)
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 22
    return row + 1


def _fila_meta(ws, row, label, value, bg_label, ncols_value=4):
    _c(ws, row, 1, label, bold=True, bg=bg_label, align_h="right", border=False)
    ws.merge_cells(
        start_row=row, start_column=2,
        end_row=row,   end_column=1 + ncols_value,
    )
    _c(ws, row, 2, value, bg=BLANCO, align_h="left", border=False)
    ws.row_dimensions[row].height = 15
    return row + 1


def _separador_nodo(ws, row, texto, color, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncols)
    c = ws.cell(row=row, column=1, value=texto)
    c.font      = Font(bold=True, color=BLANCO, size=10, name="Calibri")
    c.fill      = PatternFill("solid", fgColor=color)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[row].height = 16
    return row + 1


def _normalizar_codigo_caso(valor) -> str:
    """Normaliza LCL/ODM para comparar SIPWEBORA contra SGS."""
    if valor in (None, False):
        return ""
    texto = str(valor).strip().upper()
    if not texto or texto in {"—", "-"}:
        return ""
    texto = re.sub(r'[^0-9A-Z]', '', texto)
    # SIPWEBORA rellena con '0' los campos sin dato (LCL/ODM en alto impacto)
    if texto and set(texto) == {"0"}:
        return ""
    return texto


def _extraer_codigos_caso(valor) -> set:
    """
    Extrae identificadores de caso conocidos desde texto libre.
    Ejemplos: 6300998574, DF202..., textos que contienen esos valores.
    """
    texto = _normalizar_codigo_caso(valor)
    if not texto:
        return set()
    codigos = {texto}
    codigos.update(re.findall(r'630\d{7,}', texto))
    codigos.update(re.findall(r'DF\d{6,}', texto))
    return {c for c in codigos if c}


# Tramo inline dentro de un solo "NODO X": "NODO 4-23", "NODO 4 AL 23", "N4_N23".
_RE_NODO_SEG = re.compile(
    r'\bNODOS?\s+'
    r'([0-9][0-9A-Z]*(?:\s*(?:_|-|/|\\|\bAL\b|\bA\b)\s*N?[0-9][0-9A-Z]*)?)',
    re.IGNORECASE,
)

# Cada extremo "NODO <n>" suelto. Captura todos los nodos nombrados en el texto,
# incluso cuando SGS repite la palabra: "NODO 4- NODO 23", "NODO 4 NODO 23".
_RE_NODO_EP = re.compile(r'\bNODOS?\s*([0-9][0-9A-Z]*)', re.IGNORECASE)

# Cable escrito como tramo: "TRAMO N2 AL N5", "TRAMO RED N3 AL N7", "TRAMO CABLE N1 AL
# N9", "Tramo N1al N4", "_TRAMO N42-43_". Captura los dos extremos. Sin separador claro
# ("TRAMO RED N1A N3": ¿1A-3 o 1-3?) no se adivina.
_RE_TRAMO = re.compile(
    r'(?<![A-Z])TRAMO(?:\s+(?:RED|CABLE))?[\s_.:-]*'
    r'N?\s*([0-9]{1,4}[A-Z]?)\s*(?:_|-|/|AL|\bA\b)\s*N?\s*([0-9]{1,4}[A-Z]?)(?![0-9])')

# Códigos del caso y del ticket dentro del nombre de un proyecto SGS (LCL6301017280,
# 6301017280, DF202664695059, CDOPP2026R04493, CDOPPI202674982): se quitan para leer
# el nodo que queda.
_RE_CODIGO_EN_NOMBRE = re.compile(r'(?:LCL|DF)?[\s_]*\d{7,}|CDOPP\w*')

# Clave de nodo bien formada: 1, 25A, 4AL23, 6AL1AL9, 611. Lo demás ("NO", "PRUEBA",
# "TRAMO RED N1A N3", "1AL" cojo) es un nombre que el cruce no sabe leer.
_RE_CLAVE_NODO = re.compile(r'^(?!.*AL$)\d{1,4}[A-Z]{0,2}(?:AL\d{1,4}[A-Z]{0,2})*$')


def _nodo_key_sgs(nodo: dict) -> str:
    """
    Clave de nodo SGS robusta para cruzar contra SIPWEBORA.

    El campo `nodo` de SGS es poco fiable para cables: solo guarda un extremo
    ('23' para un cable 4-23). El nombre del proyecto sí trae el cable, pero SGS
    lo escribe de formas inconsistentes según quién lo creó:
        "NODO 4-23"        tramo inline           → 4AL23
        "NODO 4 AL 23"     tramo inline con AL     → 4AL23
        "NODO 4- NODO 23"  palabra NODO repetida   → 4AL23
        "NODO 4 NODO 23"   repetida sin separador  → 4AL23
    Se prioriza siempre el cable que describe el nombre; si el nombre solo nombra
    un nodo se usa el campo `nodo` y, como último recurso, el propio nombre.

    Varios padres por LCL (CHANGELOG 2026-10-19): si esa lectura no da una clave de nodo bien formada, se intenta rescatar
    ("TRAMO N2 AL N5", "NODO48", "1AL 11", '6301017280-N5-N15'). Una clave que ya
    se leía bien no cambia: así ningún nodo que cruzaba deja de cruzar (SIP escribe
    igual de raro algunos, p. ej. "6 - 1.1" → 611 en los dos lados).
    """
    clave = _nodo_key_sgs_base(nodo)
    if _RE_CLAVE_NODO.match(clave):
        return clave
    return _rescatar_clave_sgs(nodo) or clave


def _nodo_key_sgs_base(nodo: dict) -> str:
    """Lectura de siempre (anterior al cambio de 2026-10-19), con el cable compacto ordenado como SIP."""
    nombre = str(nodo.get("name") or nodo.get("display_name") or "").upper()

    # 1) Tramo inline dentro de un solo "NODO X-Y".
    m = _RE_NODO_SEG.search(nombre)
    inline = _canon_cable(_normalizar_nodo_sip(m.group(1))) if m else ""
    if "AL" in inline:
        return inline

    # 2) Cable con la palabra "NODO" repetida en cada extremo → tramo entre los
    #    nodos nombrados (mínimo y máximo, sin importar el orden ni el separador).
    extremos = sorted(
        {_normalizar_nodo_sip(e) for e in _RE_NODO_EP.findall(nombre)} - {""},
        key=_nodo_endpoint_key,
    )
    if len(extremos) >= 2:
        return f"{extremos[0]}AL{extremos[-1]}"

    # 3) Nodo puntual.
    campo = _canon_cable(_normalizar_nodo_sip(str(nodo.get("nodo") or "")))
    return campo or inline or str(nodo.get("nodo") or nodo.get("name", ""))


def _rescatar_clave_sgs(nodo: dict) -> str:
    """
    Nodos que la lectura de siempre no entiende (ver CHANGELOG 2026-10-19). En orden:
      · «TRAMO [RED|CABLE] N2 AL N5» en el nombre o el campo nodo → 2AL5 (LCL6301021602)
      · campo nodo con «NODO» pegado o en minúscula: «NODO48», «nodo17» → 48, 17
      · campo nodo con el cable partido: «1AL 11»               → 1AL11 (antes '1AL')
      · el nombre sin los códigos: '6301017280-N5-N15'          → 5AL15
    Devuelve '' si nada da una clave bien formada.
    """
    nombre = str(nodo.get("name") or nodo.get("display_name") or "").upper()
    campo = str(nodo.get("nodo") or "").upper()
    candidatos = []
    for t in (nombre, campo):
        mt = _RE_TRAMO.search(t)
        if mt:
            candidatos.append(f"{mt.group(1)}AL{mt.group(2)}")
    candidatos.append(_normalizar_nodo_sip(re.sub(r'^\s*NODO\s*', '', campo)))
    candidatos.append(_normalizar_nodo_sip(
        _RE_CODIGO_EN_NOMBRE.sub(" ", nombre).strip(" -_.,;:")))
    for c in candidatos:
        clave = _canon_cable(c) if c else ""
        if _RE_CLAVE_NODO.match(clave):
            return clave
    return ""


# Cable compacto "37AL36" / "48AL25A": _normalizar_nodo_sip lo deja tal cual y SIP lo
# escribe ordenado ("36AL37", "25AAL48"), así que el nodo nunca cruzaba (ver CHANGELOG 2026-10-19).
_RE_CABLE_COMPACTO = re.compile(r'^([0-9]+[A-Z]*?)AL([0-9]+[A-Z]*)$')


def _canon_cable(clave: str) -> str:
    """Ordena los extremos de un cable compacto: 37AL36 → 36AL37 (igual que SIP)."""
    m = _RE_CABLE_COMPACTO.match(clave or "")
    if not m:
        return clave
    lo, hi = sorted((m.group(1), m.group(2)), key=_nodo_endpoint_key)
    return f"{lo}AL{hi}"


# order_number que identifica un caso de verdad (LCL 630…, DF…). Los alto impacto
# llevan '0' o '1', valor que comparten cientos de proyectos: nunca se agrupa por él.
_RE_CODIGO_CASO_REAL = re.compile(r'^(?:LCL)?630\d{7}$|^DF\d{6,14}$')


def _es_codigo_caso_real(valor) -> bool:
    return bool(_RE_CODIGO_CASO_REAL.match(_normalizar_codigo_caso(valor)))


def _es_lcl(valor) -> bool:
    """LCL programado (630…). Un DF puede repetirse en obras distintas que numeran
    sus nodos desde 1: esos padres no se juntan solos."""
    return _base_codigo_caso(valor).startswith("630") and _es_codigo_caso_real(valor)


def _pros_escritos(valor) -> list:
    """Varios PRO escritos a mano: separados por coma, punto y coma o «+». El espacio
    NO separa: «PRO 063385» partido daba '063385', que se leía como id interno."""
    return [t.strip() for t in re.split(r"[,;+]", str(valor or "")) if re.search(r"\d", t)]


def _base_codigo_caso(valor) -> str:
    """'LCL 6301000001', '6301000001.' → '6301000001' (SGS escribe el LCL de varias formas)."""
    norm = _normalizar_codigo_caso(valor)
    return norm[3:] if norm.startswith("LCL") else norm


def _duplicados_sip(mat: list) -> dict:
    """
    Filas repetidas dentro de UNA planilla SIPWEBORA: mismo nodo, código y tipo.
    Retorna {nodo: [códigos]}. La 28306 traía el nodo 28 cargado dos veces (28
    códigos dobles): el cruce suma las dos filas y el nodo salía con el doble de
    SGS sin nada que lo explicara (ver CHANGELOG 2026-10-19).
    """
    tipos = ("i_n", "i_rz", "r_ch", "r_rz", "m_a")
    vistos, dup = set(), {}
    for item in mat:
        cod = str(item.get("codigo", "")).strip()
        if not cod:
            continue
        tipo = next((k for k in tipos if item.get(k, "") != ""), None)
        nodo = str(item.get("nodo") or "").strip() or "—"
        clave = (nodo, cod, tipo)
        if clave in vistos:
            dup.setdefault(nodo, set()).add(cod)
        vistos.add(clave)
    return {n: sorted(c) for n, c in sorted(dup.items(), key=lambda kv: _nodo_sort_key(kv[0]))}


def _es_linea_viva(proyecto: dict) -> bool:
    """Mismo criterio que SGSClient.buscar_por_codigo: el departamento lo delata."""
    dept = proyecto.get("department_id")
    nombre = (dept[1] if isinstance(dept, list) else str(dept or "")).upper()
    return "LLEE" in nombre or "LINEA VIVA" in nombre or "LÍNEA VIVA" in nombre


# ── Configuración ──────────────────────────────────────────────────────────────

def cfg_desde_archivo(config_path=None):
    """
    Lee credenciales SGS desde config_sgs.json (sgs_api/) o desde variables
    de entorno. Retorna dict listo para BalanceIntegrado(**cfg).
    """
    import json
    url = os.environ.get("SGS_URL", "")
    db  = os.environ.get("SGS_DB",  "")
    login    = os.environ.get("SGS_LOGIN",    "")
    password = os.environ.get("SGS_PASSWORD", "")

    if not all([url, login, password]):
        path = config_path or os.path.join(_SGS_API_DIR, "config_sgs.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            url      = url      or cfg.get("url",      "")
            db       = db       or cfg.get("db",       "")
            login    = login    or cfg.get("login",    "")
            password = password or cfg.get("password", "")

    return {"sgs_url": url, "sgs_db": db,
            "sgs_login": login, "sgs_password": password}


# ── Matching SIP ↔ SGS ────────────────────────────────────────────────────────

def _parsear_ids_sgs(valor) -> list:
    """Normaliza proyecto_id_sgs a lista; acepta ID único o varios separados por coma/espacio."""
    if not valor:
        return []
    if isinstance(valor, (list, tuple)):
        return [str(v).strip() for v in valor if str(v).strip()]
    return [p for p in re.split(r"[,;\s]+", str(valor).strip()) if p]


def _cod_base(cod: str) -> str:
    """Código sin el sufijo 'C' (251359C → 251359). La C = material comprado por Dominion."""
    cod = (cod or "").strip()
    return cod[:-1] if (cod.endswith("C") and len(cod) > 1) else cod


def _sgs_idx_nodo(nodo: dict) -> dict:
    """
    Construye un índice de materiales SGS de un nodo indexado por código SIP.

    Estrategia de claves (en orden de prioridad):
      1. customer_default_code  → código cliente = equivalente SIP
      2. cod_alt (parte /XXXX del nombre del producto)
      3. DOM code completo como último recurso

    Adicionalmente, si un código SGS termina en "C", se indexa también
    sin el "C" para que un código SIP sin sufijo lo encuentre (regla chatarra).

    Retorna:
      {sip_key: {
          "dom":       str,   # código DOM interno Odoo
          "alt":       str,   # cod_alt (parte /XXXX)
          "cdc":       str,   # customer_default_code
          "desc":      str,   # descripción del producto
          "neto":      float, # ban_deliver - ban_devolucion (expectativa almacén)
          "consumido": float, # advance: lo que el liquidador confirmó
          "pendiente": float, # neto - consumido
          "es_c":      bool,  # True si el código SGS termina en "C"
      }}
    """
    ban_agg = _agg_bandejas(nodo)  # {dom_cod: {despachado, devuelto, ...}}

    idx = {}         # {sip_key: entry}
    dom_map = {}     # {dom_cod: entry} para resolver bandejas sin match en liq

    for m in nodo.get("liq_materiales", []):
        dom_cod, cod_alt, desc = parsear_producto(m.get("product_id"))
        cdc = (m.get("customer_default_code") or "").strip()

        consumido = float(m.get("advance", 0) or 0)
        ban = ban_agg.get(dom_cod, {"despachado": 0.0, "devuelto": 0.0})
        neto = ban["despachado"] - ban["devuelto"]

        # Elegir la clave SIP primaria: cdc > cod_alt > dom_cod
        sip_key = cdc or cod_alt or dom_cod
        es_c = sip_key.endswith("C") and len(sip_key) > 1

        entry = {
            "dom": dom_cod, "alt": cod_alt, "cdc": cdc,
            "desc": desc, "neto": neto, "consumido": consumido,
            "pendiente": neto - consumido, "es_c": es_c,
        }

        idx[sip_key] = entry
        dom_map[dom_cod] = entry

        # Código sin "C" → también apunta a este entry (para promoción desde SIP).
        # Si el nodo tiene AMBAS variantes, el alias se resuelve al final.
        if es_c:
            idx.setdefault(sip_key[:-1], entry)

        # Aliases adicionales
        if cdc and cod_alt and cod_alt != cdc:
            idx.setdefault(cod_alt, entry)
        if dom_cod not in (cdc, cod_alt):
            idx.setdefault(dom_cod, entry)

    # Bandejas sin liq_material correspondiente (no confirmadas aún)
    for dom_cod, ban in ban_agg.items():
        if dom_cod not in dom_map:
            neto = ban["despachado"] - ban["devuelto"]
            entry = {
                "dom": dom_cod, "alt": "", "cdc": "",
                "desc": "", "neto": neto, "consumido": 0.0,
                "pendiente": neto, "es_c": False,
            }
            idx.setdefault(dom_cod, entry)
            dom_map[dom_cod] = entry

    # Ambas variantes en el mismo nodo (251359 con neto 0 y 251359C con el
    # despacho real): el SIP trae siempre el código pelado y debe cruzar contra
    # la variante CON movimiento. Sufijo C = material comprado por Dominion.
    for key in [k for k in idx if k.endswith("C") and len(k) > 1]:
        e_c  = idx[key]
        base = key[:-1]
        e_b  = idx.get(base)
        if e_b is None:
            idx[base] = e_c
        elif (e_b is not e_c and abs(float(e_b["neto"] or 0)) < 0.001
              and abs(float(e_c["neto"] or 0)) > 0.001):
            idx[base] = e_c

    return idx


def _normalizar_desc(texto: str) -> str:
    """
    Normaliza descripción para matching: minúsculas, sin tildes, espacios simples,
    sin puntuación irrelevante.
    """
    if not texto:
        return ""
    # Eliminar tildes / diacríticos
    nfkd = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Minúsculas y colapsar espacios
    limpio = re.sub(r"[^\w\s]", " ", sin_tildes.lower())
    return re.sub(r"\s+", " ", limpio).strip()


def _construir_desc_idx(sgs_idx: dict) -> dict:
    """
    Construye un índice secundario {desc_normalizada: entry} a partir del índice SGS.
    Permite hacer matching por descripción cuando no hay coincidencia de código.
    """
    desc_idx = {}
    for entry in sgs_idx.values():
        nd = _normalizar_desc(entry.get("desc", ""))
        if nd and nd not in desc_idx:
            desc_idx[nd] = entry
    return desc_idx


def _buscar_en_sgs(sip_cod: str, sgs_idx: dict, intentar_c: bool = True,
                   desc_idx: dict = None, sip_desc: str = ""):
    """
    Busca el código SIP en el índice SGS aplicando todas las estrategias.

    Orden de prioridad:
      1. Exacto por código (customer_default_code / cod_alt / DOM)
      2. Código + "C"  (promoción chatarra)
      3. Código sin "C"
      4. Descripción normalizada (cuando SGS no tiene customer_default_code)

    Retorna (entry, matched_key) o (None, None) si no hay match.
    """
    # 1. Exacto
    if sip_cod in sgs_idx:
        return sgs_idx[sip_cod], sip_cod

    # 2. Con sufijo "C" (promoción)
    if intentar_c and not sip_cod.endswith("C"):
        key_c = sip_cod + "C"
        if key_c in sgs_idx:
            return sgs_idx[key_c], key_c

    # 3. Sin sufijo "C"
    if sip_cod.endswith("C") and len(sip_cod) > 1:
        key_sin_c = sip_cod[:-1]
        if key_sin_c in sgs_idx:
            return sgs_idx[key_sin_c], key_sin_c

    # 4. Matching por descripción normalizada (fallback cuando no hay código cliente)
    if desc_idx and sip_desc:
        nd_sip = _normalizar_desc(sip_desc)
        if nd_sip and nd_sip in desc_idx:
            entry = desc_idx[nd_sip]
            matched_key = entry.get("cdc") or entry.get("alt") or entry.get("dom", "")
            return entry, matched_key

    return None, None


# ── Clase principal ────────────────────────────────────────────────────────────

class BalanceIntegrado:
    """
    Clase de unión lógica entre SIPWEBORA y SGS.

    Parámetros
    ----------
    sgs_url, sgs_db, sgs_login, sgs_password : str
        Credenciales del servidor SGS (Odoo).
    """

    def __init__(self, sgs_url, sgs_db, sgs_login, sgs_password):
        if not all([sgs_url, sgs_login, sgs_password]):
            raise RuntimeError(
                "Faltan credenciales SGS. "
                "Configura sgs_api/config_sgs.json o las variables de entorno "
                "SGS_URL / SGS_LOGIN / SGS_PASSWORD."
            )
        if not sgs_db:
            sgs_db = SGSClient.detectar_db(sgs_url)
            if not sgs_db:
                raise RuntimeError(
                    "No se pudo detectar la base de datos SGS. "
                    "Configura SGS_DB o añade 'db' a config_sgs.json."
                )
        self._cliente = SGSClient(sgs_url, sgs_db, sgs_login, sgs_password)
        self._cliente.autenticar()

    # ── SIPWEBORA ─────────────────────────────────────────────────────────────

    def obtener_sipwebora(self, id_planilla: str) -> dict:
        """Descarga y parsea una planilla SIPWEBORA. Retorna {metadata, mo, mat}."""
        html = descargar_html(str(id_planilla))
        return extraer_datos(html)

    # ── SGS: resolución de proyecto ───────────────────────────────────────────

    def resolver_proyecto_sgs(self, proyecto_id=None, odm=None, ticket=None) -> int:
        """
        Obtiene el ID numérico del proyecto padre en SGS.
        Prioridad: proyecto_id explícito → OdM → Ticket.
        """
        if proyecto_id:
            sid = str(proyecto_id).strip()
            if sid.isdigit():
                return int(sid)
            p = self._cliente.buscar_por_codigo(sid)
            if not p:
                raise RuntimeError(f"Proyecto '{proyecto_id}' no encontrado en SGS.")
            return p["id"]

        for valor in [odm, ticket]:
            if valor and valor not in ("—", ""):
                p = self._cliente.buscar_por_codigo(valor)
                if p:
                    return p["id"]

        raise RuntimeError(
            "No se pudo resolver el proyecto SGS automáticamente desde "
            f"OdM='{odm}' / Ticket='{ticket}'. "
            "Ingresa el ID o código SGS manualmente."
        )

    def _merge_sgs_data(self, parent_ids: list) -> dict:
        """Combina múltiples proyectos SGS padre en un único sgs_data para cruce unificado."""
        datasets = [self._cliente.balance_completo(pid) for pid in parent_ids]
        todos_nodos = []
        vistos = set()
        for ds in datasets:
            p = ds["padre"]
            for nodo in ds["nodos"]:
                # «PRO-P + PRO-N» con N hijo de P: N llega dos veces y sus bandejas se
                # sumarían dos veces en el neto que escribe el Liquidador (ver CHANGELOG 2026-10-19).
                if nodo["id"] in vistos:
                    continue
                vistos.add(nodo["id"])
                nodo["_padre_id"]   = p["id"]
                nodo["_padre_code"] = p.get("code", str(p["id"]))
                nodo["_padre_name"] = p.get("name", "")
                todos_nodos.append(nodo)
        padres = [ds["padre"] for ds in datasets]
        merged = dict(padres[0])
        merged["name"]        = "  +  ".join(p.get("name", str(p["id"])) for p in padres)
        merged["code"]        = " + ".join(p.get("code", str(p["id"])) for p in padres)
        # Mismo LCL en todos los padres: una sola vez, comparando normalizado
        # ('6301021117' = 'LCL6301021117'). Va al nombre del archivo.
        unicos = {}
        for p in padres:
            v = str(p.get("order_number") or p.get("odm") or p["id"])
            unicos.setdefault(_base_codigo_caso(v) or v, v)
        merged["order_number"] = " + ".join(unicos.values())
        merged["_merged"]     = True
        merged["_all_padres"] = padres
        return {"padre": merged, "nodos": todos_nodos}

    # Tope de padres que se juntan en un cruce: el PRO del caso (sgo7_casos.pro_code)
    # admite 60 caracteres y cuatro códigos unidos ocupan 49.
    MAX_PADRES = 4

    def _padres_mismo_caso(self, parent_id) -> tuple:
        """
        Proyectos padre SGS que comparten el LCL (630…) del proyecto resuelto.

        SGS a veces parte un mismo LCL en dos padres: 6301017280 tiene PRO-063385
        («6301017280.», 50 nodos) y PRO-061358 («6301017280», 41 nodos). El cruce
        solo leía el primero y los nodos del otro (el 28, entre ellos) desaparecían
        (ver CHANGELOG 2026-10-19). Solo aplica si lo resuelto es un padre
        (sin parent_id) con un LCL 630…; si el usuario eligió un nodo, se respeta. Un
        DF no: obras distintas pueden compartirlo y numerar sus nodos desde 1. Línea
        Viva no se mezcla con programado.

        Retorna (ids, omitidos): ids empieza por parent_id; omitidos son los padres
        que quedaron fuera por MAX_PADRES (se avisan, nunca se callan).
        """
        cli = self._cliente
        pid = int(parent_id)
        r = cli.search_read("project.dominion", [["id", "=", pid]],
                            cli.CAMPOS_BUSQUEDA_MIN, limit=1)
        if not r or r[0].get("parent_id"):
            return [pid], []
        propio = r[0]
        if not _es_lcl(propio.get("order_number")):
            return [pid], []
        # ilike + comparación normalizada: '6301017280', 'LCL6301017280',
        # 'LCL 6301017280' o '6301017280.' son el mismo caso.
        base = _base_codigo_caso(propio.get("order_number"))
        otros = cli.search_read(
            "project.dominion",
            [["order_number", "ilike", base], ["parent_id", "=", False], ["id", "!=", pid]],
            cli.CAMPOS_BUSQUEDA_MIN, limit=50,
        )
        lv = _es_linea_viva(propio)
        otros = sorted((o for o in otros
                        if _base_codigo_caso(o.get("order_number")) == base
                        and _es_linea_viva(o) == lv), key=lambda o: o["id"])
        ids = [pid] + [o["id"] for o in otros]
        cupo = ids[:self.MAX_PADRES]
        omitidos = [f"{o.get('code')} ({o.get('name')})" for o in otros if o["id"] not in cupo]
        return cupo, omitidos

    # ── Equivalencias de nodo ──────────────────────────────────────────

    _EQ_TOL_REL = 0.005     # 0,5 % (o media unidad) entre la cantidad SIP y el neto SGS

    def _detectar_equivalencias(self, sip_datos: dict, sgs_data: dict, nodos_todos: list) -> list:
        """
        Nodos SIP sin nodo SGS del mismo nombre ↔ nodos SGS de los padres consultados sin nodo
        SIP del mismo nombre, con los MISMOS materiales y cantidades (neto despachado).

        Regla (determinista):
          · al menos el 80 % de los materiales SIP del nodo (sin chatarra) está en el nodo SGS
            con la misma cantidad, y eso cubre al menos la mitad de lo que el nodo SGS despachó;
          · un nodo SIP de un solo material solo se empareja con un nodo SGS de uno o dos
            materiales (un tramo de cable), para no casar por azar un perno suelto;
          · empate entre dos nodos SGS, o un nodo SGS pedido por dos nodos SIP → «ambigua»:
            se avisa pero no se puede aprobar.
        Devuelve [{sip, sgs, pro, padre, materiales:[{cod, sip, sgs, consumido}], resumen,
                   consumido_total, estado:'pendiente', ambigua:[...], _grupo:[nodos SGS]}].
        """
        tol = lambda q: max(0.5, self._EQ_TOL_REL * abs(q))
        sip_nodos = {}
        claves_sip_todas = set()
        for (nd, cod), v in self._sip_mat_idx(sip_datos["mat"]).items():
            claves_sip_todas.add(nd)
            if nd == "—" or v.get("tipo") in CHATARRA_TIPOS or str(cod).startswith("970"):
                continue
            try:
                q = float(v["cantidad"])
            except (TypeError, ValueError):
                continue
            if q <= 0:
                continue
            m = sip_nodos.setdefault(nd, {})
            m[_cod_base(cod)] = m.get(_cod_base(cod), 0.0) + q
        # Nodo SIP «solo»: ninguno de sus materiales cruza por nombre (el nodo SGS no existe,
        # o existe vacío — p. ej. el gemelo que creó «Crear nodos» el 5 oct para 22AL28 —, o
        # trae otra cosa). Si al menos un material cruza por nombre, el nodo es ese.
        neto_por_clave = {}
        for n in sgs_data["nodos"]:
            m = neto_por_clave.setdefault(_nodo_key_sgs(n), {})
            for e in self._sgs_dedup_nodo(n):
                cb = _cod_base(e["cod_base"])
                m[cb] = m.get(cb, 0.0) + float(e["neto"] or 0)
        fuera = sgs_data.get("_nodos_fuera") or {}
        solos = []
        for nd, mats in sip_nodos.items():
            if (fuera.get(nd) or {}).get("alcance"):
                continue
            mismo = neto_por_clave.get(nd, {})
            if any(mismo.get(c, 0.0) > 0.001 for c in mats):   # ya cruza (aunque sea con DIFER)
                continue
            solos.append(nd)
        if not solos:
            return []

        grupos = {}
        for n in nodos_todos:
            k = _nodo_key_sgs(n)
            if k and k not in claves_sip_todas:
                grupos.setdefault(k, []).append(n)
        candidatos = []
        for k, grupo in grupos.items():
            nodo = self._merge_nodos_duplicados(grupo)[0] if len(grupo) > 1 else grupo[0]
            neto, cons = {}, {}
            for e in self._sgs_dedup_nodo(nodo):
                cb = _cod_base(e["cod_base"])
                neto[cb] = neto.get(cb, 0.0) + float(e["neto"] or 0)
                cons[cb] = cons.get(cb, 0.0) + float(e["consumido"] or 0)
            neto = {c: q for c, q in neto.items() if q > 0.001}
            if neto:
                candidatos.append((k, nodo, grupo, neto, cons))

        props = []
        for nd in solos:
            mats = sip_nodos[nd]
            res = []
            for k, nodo, grupo, neto, cons in candidatos:
                iguales = [c for c, q in mats.items() if c in neto and abs(neto[c] - q) <= tol(q)]
                if not iguales:
                    continue
                if len(iguales) / len(mats) < 0.8 or len(iguales) / len(neto) < 0.5:
                    continue
                if len(mats) == 1 and len(neto) > 2:
                    continue
                res.append((len(iguales) / len(mats), len(iguales), k, nodo, grupo, neto, cons, iguales))
            if not res:
                continue
            res.sort(key=lambda t: (-t[0], -t[1], t[2]))
            top = res[0]
            empate = [t[2] for t in res[1:] if (t[0], t[1]) == (top[0], top[1])]
            _, _, k, nodo, grupo, neto, cons, iguales = top
            materiales = [{"cod": c, "sip": mats[c], "sgs": neto.get(c, 0.0), "consumido": cons.get(c, 0.0)}
                          for c in sorted(mats)]
            props.append({
                "sip": nd, "sgs": k, "pro": str(nodo.get("code") or ""),
                "padre": str(nodo.get("_padre_code") or sgs_data["padre"].get("code") or ""),
                "nombre": str(nodo.get("name") or ""),
                "materiales": materiales,
                "resumen": " · ".join(f"{m['cod']} {self._fmt_qty(m['sip'])} = {self._fmt_qty(m['sgs'])}"
                                      for m in materiales if m["cod"] in iguales),
                "consumido_total": all(m["consumido"] >= m["sgs"] - 0.001 for m in materiales if m["cod"] in iguales),
                "estado": "pendiente", "por": "", "en": "",
                "ambigua": empate, "_grupo": grupo,
            })
        # Un nodo SGS que reclaman dos nodos SIP tampoco se puede aprobar a ciegas
        usos = {}
        for p in props:
            usos.setdefault(p["sgs"], []).append(p["sip"])
        for p in props:
            otros = [s for s in usos[p["sgs"]] if s != p["sip"]]
            if otros:
                p["ambigua"] = sorted(set(p["ambigua"]) | {f"SIP {s}" for s in otros})
        return sorted(props, key=lambda p: _nodo_sort_key(p["sip"]))

    def _aplicar_equivalencias(self, sip_datos: dict, sgs_data: dict, nodos_todos: list) -> None:
        """Marca cada propuesta con la decisión guardada y aplica las aprobadas: el nodo SIP se
        cruza como el nodo SGS y ese nodo entra al cruce y a SGS_BALANCE (de donde el
        Liquidador toma el PRO y el neto)."""
        props = self._detectar_equivalencias(sip_datos, sgs_data, nodos_todos)
        clave = equiv_clave_caso(sgs_data["padre"])
        dec = equiv_cargar().get(clave, {}) if clave else {}
        alias = {}
        claves_sgs = {_nodo_key_sgs(n) for n in sgs_data["nodos"]}
        for p in props:
            d = dec.get(p["sip"])
            if d and d.get("sgs") == p["sgs"] and d.get("estado") in ("aprobada", "rechazada"):
                p.update(estado=d["estado"], por=d.get("por", ""), en=d.get("en", ""))
            grupo = p.pop("_grupo")
            if p["estado"] == "aprobada" and not p["ambigua"]:
                alias[p["sip"]] = p["sgs"]
                if p["sgs"] not in claves_sgs:
                    sgs_data["nodos"] = list(sgs_data["nodos"]) + self._merge_nodos_duplicados(grupo)
                    sgs_data["_nodos_proyecto"] = list(sgs_data.get("_nodos_proyecto") or []) + list(grupo)
                    claves_sgs.add(p["sgs"])
        sgs_data["_equivalencias"] = props
        sgs_data["_equiv_clave"] = clave
        sip_datos["_alias_nodos"] = alias

    def _sip_idx_cruce(self, sip_datos: dict) -> dict:
        """Índice SIP de los cruces con las equivalencias APROBADAS aplicadas."""
        idx = self._sip_mat_idx(sip_datos["mat"])
        alias = sip_datos.get("_alias_nodos") or {}
        if not alias:
            return idx
        out = {}
        for (nd, cod), v in idx.items():
            k = (alias.get(nd, nd), cod)
            if k in out:
                out[k] = dict(out[k], cantidad=float(out[k]["cantidad"]) + float(v["cantidad"]))
            else:
                out[k] = dict(v)
        return out

    def _hoja_equivalencias(self, wb, sgs_data):
        """EQUIVALENCIAS: lo que el cruce propuso y lo que decidió la facturadora. Solo material: la
        MO no cambia (se valida a mano; decisión del 22 oct 2026)."""
        props = sgs_data.get("_equivalencias") or []
        if not props:
            return
        NCOLS = 10
        ws = wb.create_sheet("EQUIVALENCIAS")
        ws.sheet_view.showGridLines = False
        for i, w in enumerate([12, 12, 16, 16, 44, 54, 14, 14, 22, 17], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        r = _titulo_hoja(ws, 1, "EQUIVALENCIAS — mismo tramo, otro nombre en SIP y en SGS", C_HDR_FACT, NCOLS)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
        c = ws.cell(row=r, column=1, value=(
            "  El nodo SIP no cruza con SGS por nombre (no existe, o existe sin esos materiales), pero un nodo SGS del mismo LCL tiene los mismos "
            "materiales y cantidades. La facturadora decide en la previsualización del Balance. APROBADA: el "
            "cruce compara el material de ese nodo SIP contra el nodo SGS, y ese nodo entra con su PRO a SGS_BALANCE y "
            "CRUCE_LIQUIDADOR, de donde el Liquidador toma el material. La mano de obra no cambia."))
        c.font = Font(size=9, italic=True, name="Calibri")
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[r].height = 40
        r += 2
        hdr = ["Nodo SIP", "Nodo SGS", "PRO SGS", "PRO padre", "Nombre en SGS",
               "Materiales (SIP = SGS)", "Ya consumido", "Estado", "Decidió", "Fecha"]
        for i, h in enumerate(hdr, 1):
            _c(ws, r, i, h, bold=True, bg=C_SUBHDR, align_h="center", size=9)
        r += 1
        for p in props:
            est = "AMBIGUA" if p.get("ambigua") else p["estado"].upper()
            bg = {"APROBADA": C_VERDE_SGS, "RECHAZADA": C_PAR, "AMBIGUA": C_AMBAR}.get(est, C_EQUIV)
            vals = [p["sip"], p["sgs"], p["pro"], p["padre"], p.get("nombre", ""),
                    p["resumen"] + (f"  ·  también se parece a: {', '.join(p['ambigua'])}" if p.get("ambigua") else ""),
                    "Sí" if p.get("consumido_total") else "No", est, p.get("por", ""), p.get("en", "")]
            for i, v in enumerate(vals, 1):
                _c(ws, r, i, v, bg=bg if i == 8 else BLANCO, align_h="left" if i in (5, 6) else "center",
                   size=9, wrap=i in (5, 6), bold=i in (1, 2, 8))
            ws.row_dimensions[r].height = 28
            r += 1

    def _nodos_sgs_fuera(self, sip_datos: dict, sgs_data: dict) -> dict:
        """
        Nodos SIP que no están en los proyectos consultados: ¿existen en SGS?

        Retorna {clave_nodo: {"alcance": bool, "proyectos": [{"code", "name", "padre"}]}}
          alcance=True   están en otro proyecto del MISMO LCL que no se consultó (el
                         usuario eligió un nodo, o el LCL pasa de MAX_PADRES): aviso INFO,
                         no cuenta como descuadre.
          alcance=False  cuelgan de un padre que no es de este LCL (6301017280-N5-N15
                         bajo PRO-063006, un DF de otro caso) o son del otro tipo (Línea
                         Viva frente a programado): en rojo, el CRITICO dice dónde están.
        Un nodo que no aparece en ningún lado no sale aquí (el cruce lo pinta en rojo).
        """
        claves_sgs = {_nodo_key_sgs(n) for n in sgs_data.get("nodos", [])}
        faltan = {nd for (nd, _cod) in self._sip_mat_idx(sip_datos.get("mat", []))} - claves_sgs - {"—"}
        if not faltan:
            return {}
        consultados = sgs_data["padre"].get("_all_padres") or [sgs_data["padre"]]
        # El código sale de cada padre por separado (el unido no es un código) o del LCL SIP.
        codigo = next(
            (c for c in [p.get("order_number") for p in consultados]
             + [sip_datos.get("metadata", {}).get("lcl")]
             if _es_codigo_caso_real(c)),
            None,
        )
        if not codigo:
            return {}
        base = _base_codigo_caso(codigo)
        ids_consultados = {p.get("id") for p in consultados}
        ya = {n.get("id") for n in sgs_data.get("nodos", [])} | ids_consultados
        try:
            candidatos = self._cliente.search_read(
                "project.dominion",
                ["|", ["order_number", "ilike", base], ["name", "ilike", base]],
                ["id", "code", "name", "nodo", "parent_id", "order_number", "department_id"],
                limit=300,
            )
        except RuntimeError:
            return {}
        # Familia del LCL que NO se consultó: el padre de un nodo elegido a mano (sus
        # hermanos son del mismo caso aunque el padre tenga order_number '1') y todo
        # proyecto sin padre con el LCL en el order_number o en el nombre, del mismo
        # tipo (Línea Viva no es familia de programado ni al revés).
        lv = _es_linea_viva(consultados[0])
        familia = {p["parent_id"][0] for p in consultados if isinstance(p.get("parent_id"), list)}
        familia.update(c["id"] for c in candidatos
                       if not c.get("parent_id") and _es_linea_viva(c) == lv)
        familia -= ids_consultados
        fuera = {}
        for p in candidatos:
            if p.get("id") in ya:
                continue
            clave = _nodo_key_sgs(p)
            if clave not in faltan:
                continue
            padre = p.get("parent_id")
            padre_id = padre[0] if isinstance(padre, list) else None
            if padre_id in ids_consultados:
                continue    # del proyecto consultado: el cruce ya lo ve con esa clave
            alcance = (padre_id in familia) if padre_id else (p.get("id") in familia)
            info = fuera.setdefault(clave, {"alcance": False, "proyectos": []})
            info["alcance"] = info["alcance"] or alcance
            info["proyectos"].append({
                "code": p.get("code", ""), "name": p.get("name", ""),
                "padre": padre[1] if isinstance(padre, list) else "",
                "alcance": alcance,
            })
        return fuera

    @staticmethod
    def _sumar_montos(valores: list) -> str:
        """Suma strings monetarios tipo '$1,234.56' y devuelve el mismo formato."""
        total = 0.0
        for v in valores:
            if not v:
                continue
            try:
                total += float(str(v).replace("$", "").replace(",", "").strip())
            except (ValueError, TypeError):
                pass
        return f"${total:,.2f}"

    def _merge_sip_datos(self, ids_planilla: list) -> dict:
        """
        Descarga y fusiona múltiples planillas SIPWEBORA que pertenecen al mismo
        proyecto Sony (mismo LCL/PDL) pero fueron separadas en SIPWEBORA.

        Estrategia de merge de materiales:
          - Agrupa por (nodo, codigo, tipo_activo) y suma cantidades.
          - Concatena todas las filas de mano de obra.
          - Metadata: usa la primera planilla como base; total_mo y total_mat se suman.

        IMPORTANTE: el nodo es parte de la clave. Sin él, un mismo código que vive
        en varios nodos (lo normal: 201020 está en 9 nodos, 251363 en 20) se
        colapsaba en el primer nodo sumando todas las cantidades, inflando ese nodo
        y vaciando los demás → el cruce con SGS dejaba de "hablar el mismo idioma".
        """
        TIPO_KEYS = ["i_n", "i_rz", "r_ch", "r_rz", "m_a"]

        datasets = [self.obtener_sipwebora(p) for p in ids_planilla]

        meta = dict(datasets[0]["metadata"])
        meta["_planillas"] = ids_planilla
        meta["_duplicados"] = [
            (str(pl), nodo, cods)
            for pl, ds in zip(ids_planilla, datasets)
            for nodo, cods in _duplicados_sip(ds["mat"]).items()
        ]
        meta["total_mo"]  = self._sumar_montos([ds["metadata"].get("total_mo")  for ds in datasets])
        meta["total_mat"] = self._sumar_montos([ds["metadata"].get("total_mat") for ds in datasets])

        all_mo: list = []
        for ds in datasets:
            all_mo.extend(ds["mo"])

        merged_mat: dict = {}
        for ds in datasets:
            for item in ds["mat"]:
                cod = str(item.get("codigo", "")).strip()
                if not cod:
                    continue
                tipo_activo = next(
                    (k for k in TIPO_KEYS if item.get(k, "") != ""), None
                )
                nodo = str(item.get("nodo") or "").strip()
                key = (nodo, cod, tipo_activo)
                if key not in merged_mat:
                    merged_mat[key] = dict(item)
                elif tipo_activo:
                    try:
                        ex  = float(str(merged_mat[key].get(tipo_activo) or 0))
                        nw  = float(str(item.get(tipo_activo)             or 0))
                        tot = ex + nw
                        merged_mat[key][tipo_activo] = int(tot) if tot == int(tot) else tot
                    except (ValueError, TypeError):
                        pass

        return {"metadata": meta, "mo": all_mo, "mat": list(merged_mat.values())}

    @staticmethod
    def validar_coincidencia_caso(meta: dict, padre: dict) -> list:
        """
        Advierte si el caso SIPWEBORA no parece corresponder al proyecto SGS.

        Para programados, el LCL SIP normalmente coincide con order_number SGS
        (ej. 6300...). Para alto impacto, LCL/ODM vienen en '0' y el DF viaja
        en el campo ticket.
        """
        sip_codigos = set()
        for campo in ("lcl", "odm", "ticket"):
            sip_codigos.update(_extraer_codigos_caso(meta.get(campo)))

        # Con padres juntados se mira cada uno por separado: el texto unido
        # ('6301017280.  +  6301017280') se leería como un solo código de 20 dígitos.
        por_padre = []
        for p in (padre.get("_all_padres") or [padre]):
            cods = set()
            for campo in ("order_number", "odm", "name", "display_name"):
                cods.update(_extraer_codigos_caso(p.get(campo)))
            por_padre.append((p, cods))
        sgs_codigos = set().union(*(c for _p, c in por_padre))

        if not sip_codigos or not sgs_codigos:
            return []

        if sip_codigos & sgs_codigos:
            # PRO escritos a mano: uno de otro caso no queda tapado por el que sí casa.
            ajenos = [p for p, cods in por_padre if cods and not (cods & sip_codigos)]
            if len(por_padre) > 1 and ajenos:
                txt = ", ".join(f"{p.get('code')} ({p.get('order_number') or p.get('name')})"
                                for p in ajenos)
                return [{
                    "tipo": "LCL_NO_COINCIDE",
                    "titulo": "Advertencia: un proyecto SGS no es de este LCL",
                    "mensaje": (f"{txt} no trae el LCL/ODM de la planilla SIPWEBORA. "
                                "Revisa los PRO escritos antes de usar el cruce."),
                    "sip": meta.get("lcl") or meta.get("odm") or "",
                    "sgs": txt,
                }]
            return []

        # Primer campo SIP con un código real (evita mostrar el '0' de relleno)
        sip_txt = next(
            (meta.get(c) for c in ("lcl", "odm", "ticket")
             if _extraer_codigos_caso(meta.get(c))),
            "sin dato",
        )
        sgs_txt = (
            padre.get("order_number")
            or padre.get("odm")
            or padre.get("name")
            or padre.get("display_name")
            or "sin dato"
        )
        return [{
            "tipo": "LCL_NO_COINCIDE",
            "titulo": "Advertencia: LCL SIPWEBORA y proyecto SGS no coinciden",
            "mensaje": (
                f"SIPWEBORA trae LCL/ODM '{sip_txt}', pero el proyecto SGS resuelto "
                f"trae '{sgs_txt}'. Revisa el ID de planilla SIP y el PRO/SGS antes "
                "de usar el cruce."
            ),
            "sip": sip_txt,
            "sgs": sgs_txt,
        }]

    # ── Flujo principal ───────────────────────────────────────────────────────

    def ejecutar(self, id_planilla: str, proyecto_id_sgs=None) -> dict:
        """
        Orquesta el flujo completo.

        1. Descarga planilla SIPWEBORA.
        2. Resuelve el/los proyecto(s) en SGS (por OdM/Ticket extraídos, o id(s) explícito(s)).
        3. Obtiene balance completo desde SGS; si hay múltiples IDs los combina.
        4. Genera Excel integrado.

        proyecto_id_sgs puede ser un ID único ("49879"), un código ("PRO-052651"),
        o varios IDs separados por coma/espacio ("18505, 18374") para proyectos
        Sony que comparten LCL/PDL pero fueron separados en SGS.

        Retorna
        -------
        dict con claves:
          excel_bytes    : bytes del Excel generado
          filename       : nombre de archivo sugerido
          metadata       : dict de metadatos SIPWEBORA
          sgs_padre      : dict del proyecto padre en SGS (o merged si varios)
          nodos_resumen  : list[dict] — resumen por nodo SGS
        """
        datos    = self.preparar_cruce(id_planilla, proyecto_id_sgs)
        ts       = datetime.now().strftime("%Y%m%d_%H%M")
        padre    = datos["sgs_padre"]
        odm_sgs  = padre.get("order_number") or padre.get("odm") or datos["parent_id"]
        filename = f"balance_integrado_{datos['id_label']}_{odm_sgs}_{ts}.xlsx"

        excel_bytes = self._generar_excel(
            datos["sip_datos"], datos["sgs_data"], datos["id_label"],
            advertencias=datos["advertencias"],
        )

        return {
            "excel_bytes":   excel_bytes,
            "filename":      filename,
            "metadata":      datos["metadata"],
            "sgs_padre":     padre,
            "nodos_resumen": datos["nodos_resumen"],
            "advertencias":  datos["advertencias"],
        }

    def preparar_cruce(self, id_planilla: str, proyecto_id_sgs=None) -> dict:
        """
        Corre el cruce SIP↔SGS y devuelve los datos crudos SIN generar Excel.

        Reutilizado por el flujo de previsualización: primero se preparan los
        datos (para mostrarlos en pantalla y que el usuario verifique), y solo
        al final se generan los Excel (balance integrado + RG11 supervisor).
        """
        # Sin repetidos: «28306, 28306» sumaba la planilla dos veces.
        ids_sip   = list(dict.fromkeys(_parsear_ids_sgs(id_planilla)))
        multi_sip = len(ids_sip) > 1

        if multi_sip:
            sip_datos = self._merge_sip_datos(ids_sip)
            id_label  = "+".join(ids_sip)
        else:
            sip_id    = ids_sip[0] if ids_sip else id_planilla
            sip_datos = self.obtener_sipwebora(sip_id)
            id_label  = sip_id
            sip_datos["metadata"]["_duplicados"] = [
                (str(sip_id), nodo, cods)
                for nodo, cods in _duplicados_sip(sip_datos["mat"]).items()
            ]

        meta = sip_datos["metadata"]

        # Varios PRO escritos a mano («PRO-063385 + PRO-061358», «18505, 18374», el
        # código que muestra el panel tras juntar padres): se juntan tal cual, sin
        # buscar hermanos. El docstring de ejecutar() lo prometía y fallaba (ver CHANGELOG 2026-10-19).
        pros = _pros_escritos(proyecto_id_sgs)
        if len(pros) > 1:
            padres_ids = list(dict.fromkeys(
                self.resolver_proyecto_sgs(proyecto_id=t) for t in pros))
            parent_id = padres_ids[0]
            padres_omitidos = [str(t) for t in padres_ids[self.MAX_PADRES:]]
            padres_ids = padres_ids[:self.MAX_PADRES]
        else:
            parent_id = self.resolver_proyecto_sgs(
                proyecto_id=pros[0] if pros else proyecto_id_sgs,
                odm=meta.get("odm", ""),
                ticket=meta.get("ticket", ""),
            )
            # Todos los padres SGS del mismo LCL (ver CHANGELOG 2026-10-19): sin esto, los nodos del
            # segundo padre no entraban al cruce y desaparecían sin aviso.
            try:
                padres_ids, padres_omitidos = self._padres_mismo_caso(parent_id)
            except Exception as e:   # nunca tumba el Balance: queda como antes
                print("balance: _padres_mismo_caso falló:", e)
                padres_ids, padres_omitidos = [int(parent_id)], []
        if len(padres_ids) > 1:
            sgs_data = self._merge_sgs_data(padres_ids)
        else:
            sgs_data = self._cliente.balance_completo(parent_id)
        # El proyecto padre SGS suele traer muchos más nodos de los que están en
        # la planilla SIP. La comparación es SIP↔SGS, así que recortamos los nodos
        # SGS a solo los que aparecen en SIP (misma normalización que usa el cruce).
        nodos_todos  = sgs_data["nodos"]
        sgs_data     = self._filtrar_nodos_por_sip(sip_datos, sgs_data)
        # Un nodo por proyecto SGS, antes de fundir gemelos: SGS_BALANCE (de donde el
        # Liquidador saca el PRO y el neto que escribe) va proyecto por proyecto. Si
        # dos proyectos del mismo nodo tienen material, cada uno lleva su propio neto
        # y no se escribe la suma en el primero (ver CHANGELOG 2026-10-19).
        nodos_proyecto = sgs_data["nodos"]
        # SGS a veces crea el mismo nodo físico en dos PRO distintos: uno vacío y
        # otro con el material real (ej. PRO-053400 '10B' sin bandejas vs
        # PRO-053170 'NODO 10B' con 4 bandejas). Sin fusión, el PRO vacío pinta
        # todo el nodo como SOLO SIP (NO EN SGS) y el PRO real repite los mismos
        # códigos más abajo (doble conteo en el cruce).
        sgs_data     = dict(
            sgs_data, nodos=self._merge_nodos_duplicados(sgs_data["nodos"])
        )
        advertencias = self.validar_coincidencia_caso(meta, sgs_data["padre"])
        if padres_omitidos:
            advertencias.append({
                "tipo": "PADRES_OMITIDOS",
                "titulo": "Advertencia: el LCL tiene más proyectos padre en SGS",
                "mensaje": (
                    f"El cruce junta {self.MAX_PADRES} proyectos padre; quedaron fuera "
                    + ", ".join(padres_omitidos)
                    + ". Escribe esos PRO en el campo del proyecto SGS para revisarlos aparte."
                ),
            })
        sgs_data["_nodos_proyecto"] = nodos_proyecto
        # Nodos SIP sin proyecto en los padres: ¿están en SGS colgados de otro padre?
        try:
            sgs_data["_nodos_fuera"] = self._nodos_sgs_fuera(sip_datos, sgs_data)
        except Exception as e:       # solo informa: si falla, el cruce sigue
            print("balance: _nodos_sgs_fuera falló:", e)
            sgs_data["_nodos_fuera"] = {}
        parcial = {k: v for k, v in sgs_data["_nodos_fuera"].items() if v["alcance"]}
        if parcial:
            advertencias.append({
                "tipo": "CRUCE_PARCIAL",
                "titulo": "Advertencia: no se cruzaron todos los nodos de la planilla",
                "mensaje": (
                    f"{len(parcial)} nodo(s) de la planilla están en otro proyecto SGS del mismo "
                    f"LCL: " + ", ".join(
                        f"{k} ({' + '.join(p['code'] for p in v['proyectos'] if p['alcance'])})"
                        for k, v in sorted(parcial.items(), key=lambda kv: _nodo_sort_key(kv[0])))
                    + ". Si se va a liquidar la planilla completa, consulta con el PRO padre."
                ),
            })
        # Nodos SGS con material cuyo nombre no se lee como nodo («NO» por «N0»,
        # «TRAMO RED N1A N3»): el filtro los saca y nunca cruzan. Se listan junto a
        # los nodos SIP sin par para que se vea que pueden ser el mismo nodo.
        sgs_data["_nodos_raros"] = [
            {"code": n.get("code", ""), "name": n.get("name", ""), "clave": _nodo_key_sgs(n)}
            for n in nodos_todos
            if not _RE_CLAVE_NODO.match(_nodo_key_sgs(n))
            and (n.get("bandejas") or n.get("liq_materiales"))
        ]
        # tramos con los mismos materiales y otro nombre (SIP 22AL28 = SGS N26_N28).
        # Las aprobadas se cruzan como el nodo SGS; las pendientes se avisan en morado.
        try:
            self._aplicar_equivalencias(sip_datos, sgs_data, nodos_todos)
        except Exception as e:      # solo informa: si falla, el cruce sigue como antes
            print("balance: equivalencias falló:", e)
            sgs_data["_equivalencias"] = []
            sip_datos["_alias_nodos"] = {}
        for eq in sgs_data.get("_equivalencias") or []:
            if eq["estado"] == "pendiente":
                advertencias.append({
                    "tipo": "EQUIVALENCIA",
                    "titulo": "Posible mismo tramo con otro nombre",
                    "mensaje": (
                        f"El nodo {eq['sip']} de la planilla no cruza con SGS por nombre (en SGS no tiene "
                        f"esos materiales), pero {eq['sgs']} ({eq['pro']}) sí los tiene: {eq['resumen']}. "
                        "Si es el mismo tramo, apruébalo en «Equivalencias»: su material se cruza y se liquida en ese nodo."
                        + (f" También se parece a {', '.join(eq['ambigua'])}." if eq.get("ambigua") else "")
                    ),
                })

        nodos_resumen = [
            {
                "pro":        n.get("code", ""),
                "nodo":       n.get("nodo", ""),
                "status":     (n.get("status", ["", ""])[1]
                               if isinstance(n.get("status"), list) else ""),
                "bandejas":   len(n.get("bandejas",       [])),
                "materiales": len(n.get("liq_materiales", [])),
            }
            for n in sgs_data["nodos"]
        ]

        return {
            "sip_datos":     sip_datos,
            "sgs_data":      sgs_data,
            "id_label":      id_label,
            "metadata":      meta,
            "sgs_padre":     sgs_data["padre"],
            "parent_id":     parent_id,
            "advertencias":  advertencias,
            "nodos_resumen": nodos_resumen,
        }

    # ── Generación de Excel ───────────────────────────────────────────────────

    def _generar_excel(
        self,
        sip_datos: dict,
        sgs_data: dict,
        id_planilla: str,
        advertencias=None,
        fecha_ejecucion=None,
        lideres=None,
    ) -> bytes:
        wb = openpyxl.Workbook()
        self._hoja_sip_mo(wb, sip_datos, id_planilla)
        if advertencias:
            self._hoja_advertencias(wb, sip_datos, sgs_data, advertencias)
        self._hoja_sip_mat(wb, sip_datos, id_planilla, sgs_data=sgs_data)
        self._hoja_sgs_balance(wb, sgs_data)
        self._hoja_cruce_facturas(wb, sip_datos, sgs_data)
        self._hoja_cruce_sergio(wb, sip_datos, sgs_data)
        self._hoja_equivalencias(wb, sgs_data)          # (solo si el cruce propuso alguna)
        if fecha_ejecucion or lideres:
            self._hoja_liquidacion(wb, sip_datos, fecha_ejecucion, lideres)
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    def _hoja_liquidacion(self, wb, sip_datos, fecha_ejecucion, lideres):
        """Hoja con la fecha real de ejecución y los líderes a liquidar con su %."""
        ws = wb.create_sheet("LIQUIDACION", 0)
        ws.sheet_view.showGridLines = False
        ws.column_dimensions["A"].width = 8
        ws.column_dimensions["B"].width = 22
        ws.column_dimensions["C"].width = 38
        ws.column_dimensions["D"].width = 16

        meta = sip_datos.get("metadata", {})
        r = _titulo_hoja(ws, 1, "DATOS DE LIQUIDACION", C_HDR_SGS, 4)
        r = _fila_meta(ws, r, "LCL/ODM:", meta.get("lcl") or meta.get("odm") or "—",
                       C_MAT_SUB, ncols_value=3)
        r = _fila_meta(ws, r, "Fecha real de ejecucion:", fecha_ejecucion or "—",
                       C_MAT_SUB, ncols_value=3)
        r += 1

        _c(ws, r, 1, "LIDERES A LIQUIDAR", bold=True, bg=C_HDR_SGS, fg=BLANCO,
           align_h="left", size=11)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        r += 1
        for col_i, h in enumerate(["#", "Cedula", "Nombre", "Porcentaje"], 1):
            _c(ws, r, col_i, h, bold=True, bg=C_SUBHDR, align_h="center", size=10)
        r += 1

        total_pct = 0.0
        for i, ld in enumerate(lideres or [], 1):
            try:
                pct = float(ld.get("pct") or 0)
            except (ValueError, TypeError):
                pct = 0.0
            total_pct += pct
            _c(ws, r, 1, i,                   align_h="center", size=10)
            _c(ws, r, 2, ld.get("cedula", ""), align_h="center", size=10)
            _c(ws, r, 3, ld.get("nombre", ""), align_h="left",   size=10)
            _c(ws, r, 4, f"{pct:g}%",          align_h="center", size=10)
            r += 1

        _c(ws, r, 3, "TOTAL", bold=True, bg=C_TOTAL, align_h="right", size=10)
        _c(ws, r, 4, f"{total_pct:g}%", bold=True,
           bg=C_TOTAL if abs(total_pct - 100) < 0.01 else C_ROJO_SGS,
           align_h="center", size=10)

    def _hoja_advertencias(self, wb, sip_datos, sgs_data, advertencias):
        ws = wb.create_sheet("ADVERTENCIAS", 0)
        ws.sheet_view.showGridLines = False
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 90

        r = _titulo_hoja(ws, 1, "ADVERTENCIAS DE CONSISTENCIA", C_ROJO, 2)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=2)
        c = ws.cell(
            row=r,
            column=1,
            value=(
                "El archivo fue generado, pero hay una diferencia importante entre "
                "la planilla SIPWEBORA y el proyecto SGS seleccionado."
            ),
        )
        c.font = Font(bold=True, color="9C0006", size=11, name="Calibri")
        c.fill = PatternFill("solid", fgColor=C_ADVERT)
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
        ws.row_dimensions[r].height = 34
        r += 2

        meta = sip_datos["metadata"]
        padre = sgs_data["padre"]
        for label, val in [
            ("LCL/ODM SIPWEBORA:", meta.get("lcl") or meta.get("odm") or "—"),
            ("Proyecto SGS:", padre.get("name", "—")),
            ("LCL/ODM SGS:", padre.get("order_number") or padre.get("odm") or "—"),
            ("Código PRO:", padre.get("code", "—")),
        ]:
            r = _fila_meta(ws, r, label, val, C_ADVERT, ncols_value=1)

        r += 1
        for adv in advertencias:
            _c(ws, r, 1, adv.get("titulo", "Advertencia"), bold=True,
               bg=C_ROJO_SGS, fg="9C0006", align_h="left", size=10, wrap=True)
            _c(ws, r, 2, adv.get("mensaje", ""), bg=C_ROJO_SGS,
               fg="9C0006", align_h="left", size=10, wrap=True)
            ws.row_dimensions[r].height = 42
            r += 1

    # ── Hoja 1: SIP_MANO_OBRA ────────────────────────────────────────────────

    def _hoja_sip_mo(self, wb, sip_datos, id_planilla):
        meta  = sip_datos["metadata"]
        mo    = sip_datos["mo"]
        ws    = wb.active
        ws.title = "SIP_MANO_OBRA"
        ws.sheet_view.showGridLines = False

        for i, w in enumerate([5, 8, 14, 55, 12, 12], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        titulo = meta.get("titulo", f"Planilla {id_planilla}")
        r = _titulo_hoja(ws, 1, f"SIPWEBORA — MANO DE OBRA  |  {titulo}", C_HDR_SIP, 6)

        for label, val in [
            ("Empresa:",      meta.get("empresa", "")),
            ("OdM / Grafo:",  meta.get("odm", "")),
            ("LCL:",          meta.get("lcl", "")),
            ("Ticket / PDL:", meta.get("ticket", "")),
            ("Zona:",         meta.get("zona_nombre") or meta.get("zona_cd", "—")),
            ("Fecha ejec.:",  meta.get("fecha_ejecucion", "")),
        ]:
            r = _fila_meta(ws, r, label, val, C_SUBHDR, ncols_value=4)

        r += 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        c = ws.cell(row=r, column=1, value="MANO DE OBRA")
        c.font      = Font(bold=True, color=BLANCO, size=11, name="Calibri")
        c.fill      = PatternFill("solid", fgColor=C_HDR_SIP)
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 18
        r += 1

        for col_i, h in enumerate(
            ["#", "Nodo", "Código", "Descripción", "Unidad", "Cantidad"], 1
        ):
            _c(ws, r, col_i, h, bold=True, bg=C_SUBHDR, align_h="center", size=9)
        ws.row_dimensions[r].height = 14
        r += 1

        for idx, item in enumerate(mo, 1):
            bg = BLANCO if idx % 2 == 0 else C_PAR
            cant = item["cantidad"]
            if isinstance(cant, float) and cant == int(cant):
                cant = int(cant)
            _c(ws, r, 1, idx,             bg=bg, align_h="center", size=9)
            _c(ws, r, 2, item["nodo"],    bg=bg, align_h="center", size=9)
            _c(ws, r, 3, item["codigo"],  bg=bg, align_h="center", size=9)
            _c(ws, r, 4, item["desc"],    bg=bg, align_h="left",   size=9, wrap=True)
            _c(ws, r, 5, item["unidad"],  bg=bg, align_h="center", size=9)
            _c(ws, r, 6, cant,            bg=bg, align_h="center", size=9)
            ws.row_dimensions[r].height = 14
            r += 1

        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=5)
        _c(ws, r, 1, "TOTAL MANO DE OBRA", bold=True, bg=C_TOTAL, align_h="right", size=10)
        _c(ws, r, 6, meta.get("total_mo", ""), bold=True, bg=C_TOTAL, align_h="center", size=10)
        ws.row_dimensions[r].height = 16

    # ── Hoja 2: SIP_MATERIALES ───────────────────────────────────────────────

    def _hoja_sip_mat(self, wb, sip_datos, id_planilla, sgs_data=None):
        """
        sgs_data opcional: si se provee, se usa para detectar materiales clasificados
        como chatarra en SIP que también existen en SGS (posible error de clasificación).
        """
        meta = sip_datos["metadata"]
        mat  = sip_datos["mat"]
        ws   = wb.create_sheet("SIP_MATERIALES")
        ws.sheet_view.showGridLines = False

        for i, w in enumerate([5, 8, 14, 52, 12, 10, 22], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        titulo = meta.get("titulo", f"Planilla {id_planilla}")
        r = _titulo_hoja(ws, 1, f"SIPWEBORA — MATERIALES  |  {titulo}", C_HDR_SGS, 7)

        for label, val in [
            ("Empresa:",      meta.get("empresa", "")),
            ("OdM / Grafo:",  meta.get("odm", "")),
            ("Ticket / PDL:", meta.get("ticket", "")),
            ("Zona:",         meta.get("zona_nombre") or meta.get("zona_cd", "—")),
        ]:
            r = _fila_meta(ws, r, label, val, C_MAT_SUB, ncols_value=5)

        r += 1
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=7)
        c = ws.cell(row=r, column=1, value="MATERIALES")
        c.font      = Font(bold=True, color=BLANCO, size=11, name="Calibri")
        c.fill      = PatternFill("solid", fgColor=C_HDR_SGS)
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 18
        r += 1

        for col_i, h in enumerate(
            ["#", "Nodo", "Código", "Material", "Cantidad", "Tipo", "Observación"], 1
        ):
            _c(ws, r, col_i, h, bold=True, bg=C_MAT_SUB, align_h="center", size=9)
        ws.row_dimensions[r].height = 14
        r += 1

        # Construir índice SGS global (desc normalizada) para validación cruzada
        sgs_desc_global = set()
        if sgs_data:
            for nodo in sgs_data.get("nodos", []):
                sgs_idx  = _sgs_idx_nodo(nodo)
                desc_idx = _construir_desc_idx(sgs_idx)
                sgs_desc_global.update(desc_idx.keys())

        tipo_map = {"i_n": "I N", "i_rz": "I RZ", "r_ch": "R CH",
                    "r_rz": "R RZ", "m_a": "M A"}
        for idx, item in enumerate(mat, 1):
            bg = BLANCO if idx % 2 == 0 else C_PAR
            tipo_key = next(
                (k for k in ["i_n", "i_rz", "r_ch", "r_rz", "m_a"] if item[k] != ""), None
            )
            cant_mat = item[tipo_key] if tipo_key else ""
            tipo_val = tipo_map.get(tipo_key, "") if tipo_key else ""
            if isinstance(cant_mat, float) and cant_mat == int(cant_mat):
                cant_mat = int(cant_mat)

            es_chatarra = tipo_val in CHATARRA_TIPOS

            # Validación: ¿este material chatarra también existe en SGS?
            desc_norm = _normalizar_desc(item["desc"])
            existe_en_sgs = es_chatarra and bool(sgs_desc_global) and desc_norm in sgs_desc_global

            if existe_en_sgs:
                # Naranja — posible error de clasificación
                bg_fila = C_ERROR_CLASIF
                obs_txt = "! ERROR CLASIFICACION"
                obs_fg  = BLANCO
            elif es_chatarra:
                # Amarillo — chatarra confirmada (no existe en SGS)
                bg_fila = C_AMARILLO
                obs_txt = "CHATARRA"
                obs_fg  = "7B3F00"
            else:
                bg_fila = bg
                obs_txt = ""
                obs_fg  = "000000"

            _c(ws, r, 1, idx,            bg=bg_fila, align_h="center", size=9,
               fg=BLANCO if existe_en_sgs else "000000")
            _c(ws, r, 2, item["nodo"],   bg=bg_fila, align_h="center", size=9,
               fg=BLANCO if existe_en_sgs else "000000")
            _c(ws, r, 3, item["codigo"], bg=bg_fila, align_h="center", size=9,
               fg=BLANCO if existe_en_sgs else "000000")
            _c(ws, r, 4, item["desc"],   bg=bg_fila, align_h="left",   size=9, wrap=True,
               fg=BLANCO if existe_en_sgs else "000000")
            _c(ws, r, 5, cant_mat,       bg=bg_fila, align_h="center", size=9,
               fg=BLANCO if existe_en_sgs else "000000")
            _c(ws, r, 6, tipo_val,       bg=bg_fila, align_h="center", size=9,
               bold=es_chatarra, fg=BLANCO if existe_en_sgs else "000000")
            _c(ws, r, 7, obs_txt,        bg=bg_fila, size=9,
               fg=obs_fg, bold=True, align_h="center")
            ws.row_dimensions[r].height = 14
            r += 1

        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        _c(ws, r, 1, "TOTAL MATERIALES", bold=True, bg=C_TOTAL, align_h="right", size=10)
        _c(ws, r, 5, meta.get("total_mat", ""), bold=True, bg=C_TOTAL, align_h="center", size=10)
        ws.row_dimensions[r].height = 16

    # ── Hoja 3: SGS_BALANCE ───────────────────────────────────────────────────

    def _hoja_sgs_balance(self, wb, sgs_data):
        """
        Balance SGS por bandeja.

        Estructura: una fila por material, en orden de bandejas deliver (BAN orden asc).
        Muestra todos los materiales aunque neto=0.

        Columnas:
          # | Código | Descripción | Bandejas Despacho | Bandejas Devolución |
          Despachos | Devoluciones | Bal. Neto | Consumido | Estado
        """
        # A  B       C             D                   E
        # #  Código  Descripción   Bandejas Despacho   Bandejas Devol.
        # F          G             H         I          J
        # Despachos  Devoluciones  Bal.Neto  Consumido  Estado
        NCOLS = 10
        HDR = ["#", "Código", "Descripción",
               "Bandejas Despacho", "Bandejas Devol.",
               "Despachos", "Devoluciones", "Bal. Neto", "Consumido", "Estado"]

        ws = wb.create_sheet("SGS_BALANCE")
        ws.sheet_view.showGridLines = False
        for i, w in enumerate([5, 14, 42, 20, 16, 11, 12, 11, 11, 16], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        padre  = sgs_data["padre"]
        nombre = padre.get("name", "")
        odm    = padre.get("order_number") or padre.get("odm", "")
        r = _titulo_hoja(ws, 1, f"SGS -- BALANCE  |  {nombre}  ({odm})", C_HDR_SGS, NCOLS)
        r = _fila_meta(ws, r, "Proyecto padre:", nombre, C_MAT_SUB, ncols_value=8)
        r = _fila_meta(ws, r, "ODM:", odm, C_MAT_SUB, ncols_value=8)
        r += 1

        idx_global = 1
        # Proyecto por proyecto (sin fundir gemelos): ver preparar_cruce (ver CHANGELOG 2026-10-19).
        for nodo in sgs_data.get("_nodos_proyecto") or sgs_data["nodos"]:
            nodo_str = _nodo_key_sgs(nodo)

            # Materiales en orden de bandejas (incluye neto=0)
            entries = self._sgs_ordenado_desde_bandejas(nodo)
            if not entries:
                continue

            # Despachos/devoluciones detallados por DOM (con listas de BANs)
            ban_agg = _agg_bandejas(nodo)


            padre_tag = nodo.get("_padre_code", "")
            pro_code  = nodo.get("code", "")
            if padre_tag:
                sep_label = f"  Nodo: {nodo_str}  [{padre_tag}]  PRO: {pro_code}"
            elif pro_code:
                sep_label = f"  Nodo: {nodo_str}  PRO: {pro_code}"
            else:
                sep_label = f"  Nodo: {nodo_str}"
            r = _separador_nodo(ws, r, sep_label, C_HDR_SGS, NCOLS)
            for col_i, h in enumerate(HDR, 1):
                _c(ws, r, col_i, h, bold=True, bg=C_MAT_SUB, align_h="center", size=9)
            ws.row_dimensions[r].height = 14
            r += 1

            for e in entries:
                dom        = e["dom"]
                # Código real de SGS: con sufijo C si es compra Dominion (custodia).
                # Antes se mostraba el base y las dos filas (251359 y 251359C)
                # salían con el mismo código, indistinguibles salvo por la descripción.
                cod        = e["cdc"] or e["alt"] or e["cod_base"]
                desc       = e["desc"] or cod
                neto       = e["neto"]
                consumido  = e["consumido"]
                agg        = ban_agg.get(dom, {
                    "despachado": 0.0, "devuelto": 0.0,
                    "ban_delivers": [], "ban_devs": [],
                })
                despachado  = agg["despachado"]
                devuelto    = agg["devuelto"]

                # Nombres de bandejas deliver y devolution (únicos, ordenados)
                bans_del = ", ".join(sorted(set(b for b, _ in agg.get("ban_delivers", [])))) or "—"
                bans_dev = ", ".join(sorted(set(b for b, _ in agg.get("ban_devs",    [])))) or "—"

                # Estado
                pendiente = neto - consumido
                if neto == 0 and consumido == 0:
                    if devuelto > 0:
                        estado, bg = "DEVOLUCION", C_VERDE_SGS
                    else:
                        estado, bg = "SIN MOV",   C_PAR
                elif neto == 0 and consumido > 0:
                    estado, bg = f"SOBRE LIQ +{consumido:.2f}", C_ROJO_SGS
                elif consumido == 0 and neto > 0:
                    estado, bg = "PENDIENTE", C_AMBAR
                elif abs(pendiente) < 0.001:
                    estado, bg = "OK", C_VERDE_SGS
                elif pendiente > 0:
                    estado, bg = f"BAJO LIQ {pendiente:+.2f}", C_AMBAR
                else:
                    estado, bg = f"SOBRE LIQ {pendiente:+.2f}", C_ROJO_SGS

                _c(ws, r, 1,  idx_global,                   bg=bg, align_h="center", size=9)
                _c(ws, r, 2,  cod,                          bg=bg, align_h="center", size=9)
                _c(ws, r, 3,  desc,                         bg=bg, align_h="left",   size=9, wrap=True)
                _c(ws, r, 4,  bans_del,                     bg=bg, align_h="center", size=8)
                _c(ws, r, 5,  bans_dev,                     bg=bg, align_h="center", size=8)
                _c(ws, r, 6,  self._fmt_qty(despachado),    bg=bg, align_h="center", size=9)
                _c(ws, r, 7,  self._fmt_qty(devuelto),      bg=bg, align_h="center", size=9)
                _c(ws, r, 8,  self._fmt_qty(neto),          bg=bg, align_h="center", size=9, bold=True)
                _c(ws, r, 9,  self._fmt_qty(consumido),     bg=bg, align_h="center", size=9)
                _c(ws, r, 10, estado,                       bg=bg, align_h="center", size=9,
                   bold=(estado == "OK"))
                ws.row_dimensions[r].height = 14
                r += 1
                idx_global += 1

        ws.freeze_panes = ws["A6"]

    # ── Hoja 4: CRUCE_FACTURAS (vista facturación) ────────────────────────────

    def _hoja_cruce_facturas(self, wb, sip_datos, sgs_data):
        """
        Vista para el equipo de facturación.

        Regla de validación de clasificación (principio de negocio):
          · SGS NUNCA maneja chatarra.
          · Si SIP clasifica como chatarra (I RZ / R CH / R RZ) PERO el material
            existe en SGS → posible error de clasificación en SIP → naranja.
          · Si SIP clasifica como chatarra Y NO existe en SGS → chatarra confirmada → amarillo.

        Colores:
          Naranja  — SIP dice chatarra PERO SGS lo tiene → ERROR CLASIFICACIÓN SIP
          Amarillo — chatarra confirmada (solo en SIP, no existe en SGS)
          Verde    — SIP qty == SGS neto (cuadra)
          Rojo     — descuadre de cantidades
          Ámbar    — sin contrapartida en uno de los sistemas
        """
        NCOLS = 10
        # A  B     C       D             E         F        G          H      I     J
        # #  Nodo  Código  Descripción   SIP Cant  SGS Neto Diferencia TipoSIP Dom   Estado
        HDR = ["#", "Nodo", "Código SIP", "Descripción",
               "SIP Cant.", "SGS Neto", "Diferencia", "Tipo SIP", "Código SGS", "Estado"]

        ws = wb.create_sheet("CRUCE_FACTURAS")
        ws.sheet_view.showGridLines = False
        for i, w in enumerate([5, 8, 14, 42, 11, 11, 11, 10, 24, 26], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        r = _titulo_hoja(
            ws, 1,
            "CRUCE SIPWEBORA ↔ SGS — Vista Facturación",
            C_HDR_FACT, NCOLS,
        )

        # Leyenda de colores
        for bg, txt in [
            (C_ERROR_CLASIF, "Naranja  — ERROR CLASIFICACION SIP: marcado chatarra en SIP pero existe en SGS (SGS nunca maneja chatarra)"),
            (C_AMARILLO,     "Amarillo — CHATARRA CONFIRMADA: en SIP como chatarra y NO existe en SGS"),
            (C_VERDE,        "Verde    — OK: cantidades SIP y SGS coinciden"),
            (C_ROJO,         "Rojo     — NO CUADRA: material SIP que no esta en SGS, o SGS despachado que no esta en SIP, o cantidades distintas"),
            (C_AMBAR,        "Ambar    — Solo SGS sin movimiento (neto 0): no se despacho, no es problema"),
            (C_AZUL_C,       "Azul (C) — Codigo SGS con sufijo C: material que ENEL no entrego y Dominion compro (se cobra aparte). 251359 = 251359C: cuadra como el mismo material"),
        ]:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
            cell = ws.cell(row=r, column=1, value=f"  {txt}")
            cell.font      = Font(size=8, italic=True, name="Calibri",
                                  color=BLANCO if bg == C_ERROR_CLASIF else "333333")
            cell.fill      = PatternFill("solid", fgColor=bg)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[r].height = 13
            r += 1
        r += 1

        # Cabecera
        for col_i, h in enumerate(HDR, 1):
            _c(ws, r, col_i, h, bold=True, bg=C_SUBHDR, align_h="center", size=9)
        ws.row_dimensions[r].height = 14
        r += 1

        # Índice SIP por nodo, normalizado al código base (sin sufijo C):
        # SIPWEBORA trae siempre el código pelado; si trajera 251359C se suma
        # al base para no duplicar filas.
        sip_idx = {}
        for (_nd, _cod), _v in self._sip_idx_cruce(sip_datos).items():   # con equivalencias aprobadas
            _key = (_nd, _cod_base(_cod))
            if _key in sip_idx:
                sip_idx[_key]["cantidad"] = (float(sip_idx[_key]["cantidad"])
                                             + float(_v["cantidad"]))
            else:
                sip_idx[_key] = dict(_v)

        # Índice global SGS (todos los nodos) para la alerta de 2 niveles:
        # distingue un material SIP ausente de TODO el proyecto SGS (crítico) de
        # uno que existe en SGS pero asignado a otro nodo (revisar).
        sgs_idx_global = {}
        sgs_cod_nodos  = {}   # clave SGS -> {labels de nodo donde aparece}
        for _n in sgs_data["nodos"]:
            _lbl = _nodo_key_sgs(_n)
            for _k, _e in _sgs_idx_nodo(_n).items():
                sgs_idx_global.setdefault(_k, _e)
                sgs_cod_nodos.setdefault(_k, set()).add(_lbl)
        desc_idx_global = _construir_desc_idx(sgs_idx_global)
        alertas_sip = []   # (severidad, nodo, cod, desc, cant, otros_nodos)
        # propuestas de equivalencia aún sin aprobar (las aprobadas ya se cruzaron)
        eq_pend = {p["sip"]: p for p in (sgs_data.get("_equivalencias") or [])
                   if p["estado"] != "aprobada" or p.get("ambigua")}

        # Contadores para resumen final
        tot_error_clasif = 0
        tot_chatarra_ok  = 0
        tot_ok           = 0
        tot_descuadre    = 0
        tot_solo         = 0
        tot_c            = 0   # filas con sufijo C (material comprado por Dominion)

        idx_global = 1
        nodos_dif  = set()   # nodos con DIFER / SOLO SIP (ver CHANGELOG 2026-10-19)
        for nodo in sgs_data["nodos"]:
            nodo_sip_key = _nodo_key_sgs(nodo)
            nodo_str     = nodo_sip_key  # etiqueta: refleja el cable real (p.ej. 4AL23)
            sgs_idx      = _sgs_idx_nodo(nodo)

            # Grupos SGS por código base: 251359 (lo entregó ENEL) y 251359C
            # (lo compró Dominion) son el MISMO material físico → UNA fila con
            # neto = suma de variantes. Antes salían dos filas falsas: la sin C
            # como "DIFER +3" (neto 0) y la C como "CHATARRA (SGS)".
            grupos = {}   # base → {"entries", "neto", "neto_c"}
            for _e in self._sgs_dedup_nodo(nodo):
                _g = grupos.setdefault(_e["cod_base"],
                                       {"entries": [], "neto": 0.0, "neto_c": 0.0})
                _g["entries"].append(_e)
                _g["neto"] += float(_e["neto"] or 0)
                if _e["es_c"]:
                    _g["neto_c"] += float(_e["neto"] or 0)

            sip_nodo = {cb: v for (nd, cb), v in sip_idx.items() if nd == nodo_sip_key}
            if not sip_nodo and not grupos:
                continue

            desc_idx = _construir_desc_idx(sgs_idx)

            # Cada código SIP → grupo SGS: por código base o, si SGS no tiene
            # customer_default_code, por descripción normalizada.
            sip_a_grupo   = {}
            grupos_usados = set()
            for cb, v in sip_nodo.items():
                gb = cb if cb in grupos else None
                if gb is None and v.get("desc"):
                    _ed, _ = _buscar_en_sgs(cb, sgs_idx, intentar_c=False,
                                            desc_idx=desc_idx, sip_desc=v["desc"])
                    if _ed is not None:
                        _gb = _cod_base(_ed["cdc"] or _ed["alt"] or _ed["dom"])
                        gb  = _gb if _gb in grupos else None
                sip_a_grupo[cb] = gb
                if gb:
                    grupos_usados.add(gb)

            r = _separador_nodo(ws, r, f"  Nodo: {nodo_str}", C_HDR_FACT, NCOLS)

            claves = sorted(set(sip_nodo) | {gb for gb in grupos if gb not in grupos_usados})
            for clave in claves:
                # Info SIP
                sip_entry = sip_nodo.get(clave)
                sip_cant  = sip_entry["cantidad"] if sip_entry else None
                sip_tipo  = sip_entry["tipo"]      if sip_entry else ""
                sip_desc  = sip_entry["desc"]      if sip_entry else ""

                # Info SGS (grupo base, con fallback por descripción)
                gb = sip_a_grupo.get(clave) if sip_entry else (clave if clave in grupos else None)
                g  = grupos.get(gb) if gb else None
                if g is None and sip_entry is None:
                    continue

                if g is not None:
                    _activos = [e for e in g["entries"] if abs(float(e["neto"] or 0)) > 0.001]
                    activo   = _activos[0] if _activos else g["entries"][0]
                    sgs_neto = g["neto"]
                    sgs_dom  = activo["dom"]
                    es_c_sgs = abs(g["neto_c"]) > 0.001
                    cods_sgs = [e["cdc"] or e["alt"] or e["dom"] for e in (_activos or [activo])]
                    desc     = activo["desc"] or sip_desc or clave
                else:
                    sgs_neto, sgs_dom, es_c_sgs, cods_sgs = None, "", False, []
                    desc = sip_desc or clave

                es_chatarra_sip = sip_tipo in CHATARRA_TIPOS
                hay_match_sgs   = g is not None and (sgs_neto or 0) != 0
                marca_c         = " (C)" if es_c_sgs else ""
                if es_c_sgs:
                    tot_c += 1

                # ── Lógica de estado y color ──────────────────────────────────
                if es_chatarra_sip and hay_match_sgs:
                    # Principio de negocio: SGS nunca maneja chatarra.
                    # Si aparece en SGS con cantidad, la clasificación SIP es sospechosa.
                    estado = "! ERROR CLASIFICACION SIP"
                    bg_row = C_ERROR_CLASIF
                    fg_est = BLANCO
                    tot_error_clasif += 1

                elif es_chatarra_sip and not hay_match_sgs:
                    # No existe en SGS → chatarra confirmada
                    estado = "CHATARRA CONFIRMADA"
                    bg_row = C_AMARILLO
                    fg_est = "7B3F00"
                    tot_chatarra_ok += 1

                elif sip_entry is None:
                    # Solo SGS. Si SGS lo despachó (neto != 0) y no está en SIP,
                    # es una discrepancia real → ROJO. Si neto = 0 (no se movió),
                    # no es problema → ámbar.
                    if (sgs_neto or 0) != 0:
                        estado = "SOLO SGS (DESPACHADO, NO EN SIP)" + marca_c
                        bg_row = C_ROJO
                        fg_est = BLANCO
                        tot_descuadre += 1
                    else:
                        estado = "SOLO SGS (sin movimiento)" + marca_c
                        bg_row = C_AMBAR
                        fg_est = "000000"
                        tot_solo += 1

                elif g is None and (eq_pend.get(nodo_str) or {}).get("estado") == "pendiente":
                    # el nodo SGS de este nombre no tiene este material (p. ej. el gemelo
                    # vacío que creó «Crear nodos»), pero otro nodo SGS tiene los mismos
                    # materiales: morado hasta que la facturadora decida. Sigue siendo descuadre.
                    estado = f"SOLO SIP — ≈ SGS {eq_pend[nodo_str]['sgs']}"
                    bg_row = C_EQUIV
                    fg_est = "000000"
                    tot_descuadre += 1

                elif g is None:
                    # REGLA: todo material instalado en SIP debe existir en SGS.
                    # Si no está → ROJO (antes ámbar, se pasaba por alto).
                    estado = "SOLO SIP (NO EN SGS)"
                    bg_row = C_ROJO
                    fg_est = BLANCO
                    tot_descuadre += 1
                    # La chatarra (970xxx) se excluye: SGS nunca la maneja.
                    if not clave.startswith("970"):
                        eg, mkg = _buscar_en_sgs(
                            clave, sgs_idx_global,
                            desc_idx=desc_idx_global, sip_desc=sip_desc,
                        )
                        if eg is not None:
                            otros = sorted(sgs_cod_nodos.get(mkg, set()) - {nodo_str})
                            alertas_sip.append(
                                ("REVISAR", nodo_str, clave, desc, sip_cant, otros))
                        else:
                            alertas_sip.append(
                                ("CRITICO", nodo_str, clave, desc, sip_cant, []))

                elif abs(float(sip_cant) - sgs_neto) < 0.001:
                    estado = "OK" + marca_c
                    bg_row = C_VERDE
                    fg_est = "000000"
                    tot_ok += 1

                else:
                    dif = float(sip_cant) - sgs_neto
                    estado = f"DIFER {dif:+.2f}" + marca_c
                    bg_row = C_ROJO
                    fg_est = "000000"
                    tot_descuadre += 1

                if estado.startswith(("DIFER", "SOLO SIP")):
                    nodos_dif.add(nodo_str)   # para la alerta de filas repetidas

                dif_val = ""
                if sip_cant is not None and sgs_neto is not None:
                    dv = round(float(sip_cant) - sgs_neto, 2)
                    dif_val = int(dv) if dv == int(dv) else dv

                # Código SGS: DOM como siempre; con sufijo C se muestra además el
                # código real (DOM-055821 / 251359C) y la celda va en azul.
                if es_c_sgs:
                    cod_sgs_display = f"{sgs_dom} / {' + '.join(cods_sgs)}"
                else:
                    cod_sgs_display = sgs_dom if sgs_dom else "—"

                _c(ws, r, 1,  idx_global,    bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 2,  nodo_str,      bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 3,  clave,         bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 4,  desc,          bg=BLANCO, align_h="left",   size=9, wrap=True)
                _c(ws, r, 5,
                   self._fmt_qty(sip_cant) if sip_cant is not None else "—",
                   bg=bg_row if es_chatarra_sip else BLANCO,
                   align_h="center", size=9)
                _c(ws, r, 6,
                   self._fmt_qty(sgs_neto) if sgs_neto is not None else "—",
                   bg=bg_row, align_h="center", size=9)
                _c(ws, r, 7,
                   dif_val if dif_val != "" else "—",
                   bg=bg_row, align_h="center", size=9,
                   num_fmt="+#,##0.##;-#,##0.##;0" if dif_val != "" else None)
                _c(ws, r, 8,  sip_tipo,      bg=C_ERROR_CLASIF if es_chatarra_sip and hay_match_sgs
                                                else (C_AMARILLO if es_chatarra_sip else BLANCO),
                   align_h="center", size=9, bold=es_chatarra_sip,
                   fg=BLANCO if (es_chatarra_sip and hay_match_sgs) else "000000")
                _c(ws, r, 9,  cod_sgs_display, bg=C_AZUL_C if es_c_sgs else BLANCO,
                   align_h="center", size=8, bold=es_c_sgs)
                _c(ws, r, 10, estado,         bg=bg_row, align_h="center", size=9,
                   bold=True, fg=fg_est)
                ws.row_dimensions[r].height = 15
                r += 1
                idx_global += 1

        # ── Nodos SIP que no existen en el proyecto SGS (ver CHANGELOG 2026-10-19) ──────────────────
        # Antes el recorrido era solo por nodos SGS y estos nodos desaparecían del
        # cruce sin una fila ni una alerta (LCL 6301017280: 34 nodos, 555 filas SIP).
        claves_sgs  = {_nodo_key_sgs(_n) for _n in sgs_data["nodos"]}
        nodos_fuera = sgs_data.get("_nodos_fuera") or {}
        alertas_nodo = []   # (nodo, n_materiales, proyectos SGS mal colgados)
        fuera_alcance = []  # (nodo, proyectos del mismo LCL que no se consultaron)
        for nodo_str in sorted({nd for (nd, _cb) in sip_idx} - claves_sgs, key=_nodo_sort_key):
            info_fuera = nodos_fuera.get(nodo_str) or {}
            if info_fuera.get("alcance"):
                # Existe en otro proyecto del mismo LCL: el usuario consultó solo
                # una parte. No es descuadre; se avisa abajo en una línea INFO.
                fuera_alcance.append((nodo_str, [p for p in info_fuera["proyectos"] if p["alcance"]]))
                continue
            sip_nodo = {cb: v for (nd, cb), v in sip_idx.items() if nd == nodo_str}
            eq = eq_pend.get(nodo_str)
            eq_activa = eq is not None and eq["estado"] == "pendiente"
            r = _separador_nodo(
                ws, r,
                "  Nodo: (sin nodo en SIP)  —  NO SE PUEDE CRUZAR" if nodo_str == "—"
                else (f"  Nodo: {nodo_str}  —  NO EXISTE EN SGS  ·  ≈ SGS {eq['sgs']} ({eq['pro']}): "
                      f"mismos materiales — ¿es el mismo tramo?" if eq_activa
                      else f"  Nodo: {nodo_str}  —  NO EXISTE EN SGS"),
                C_HDR_FACT, NCOLS)
            n_mat = 0
            for clave in sorted(sip_nodo):
                v = sip_nodo[clave]
                if v["tipo"] in CHATARRA_TIPOS:
                    estado, bg_row, fg_est = "CHATARRA CONFIRMADA", C_AMARILLO, "7B3F00"
                    tot_chatarra_ok += 1
                elif eq_activa:
                    # Sigue contando como descuadre hasta que la facturadora apruebe
                    estado, bg_row, fg_est = f"SOLO SIP — ≈ SGS {eq['sgs']}", C_EQUIV, "000000"
                    tot_descuadre += 1
                    nodos_dif.add(nodo_str)
                else:
                    estado, bg_row, fg_est = "SOLO SIP (NO EN SGS)", C_ROJO, BLANCO
                    tot_descuadre += 1
                    n_mat += 1
                    nodos_dif.add(nodo_str)
                es_chat = v["tipo"] in CHATARRA_TIPOS
                _c(ws, r, 1,  idx_global, bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 2,  nodo_str,   bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 3,  clave,      bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 4,  v["desc"] or clave, bg=BLANCO, align_h="left", size=9, wrap=True)
                _c(ws, r, 5,  self._fmt_qty(v["cantidad"]),
                   bg=bg_row if es_chat else BLANCO, align_h="center", size=9)
                _c(ws, r, 6,  "—", bg=bg_row, align_h="center", size=9)
                _c(ws, r, 7,  "—", bg=bg_row, align_h="center", size=9)
                _c(ws, r, 8,  v["tipo"], bg=C_AMARILLO if es_chat else BLANCO,
                   align_h="center", size=9, bold=es_chat)
                _c(ws, r, 9,  "—", bg=BLANCO, align_h="center", size=8)
                _c(ws, r, 10, estado, bg=bg_row, align_h="center", size=9,
                   bold=True, fg=fg_est)
                ws.row_dimensions[r].height = 15
                r += 1
                idx_global += 1
            if n_mat:
                alertas_nodo.append((nodo_str, n_mat, info_fuera.get("proyectos", [])))

        # Fila de resumen global
        r += 1
        resumen_txt = (
            f"RESUMEN — "
            f"Errores clasificacion SIP: {tot_error_clasif}  |  "
            f"Chatarra confirmada: {tot_chatarra_ok}  |  "
            f"Compra Dominion (C): {tot_c}  |  "
            f"OK: {tot_ok}  |  "
            f"Descuadre: {tot_descuadre}  |  "
            f"Solo un sistema: {tot_solo}"
        )
        bg_resumen = C_ROJO if tot_error_clasif > 0 or tot_descuadre > 0 else C_VERDE
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
        cell = ws.cell(row=r, column=1, value=resumen_txt)
        cell.font      = Font(bold=True, color=BLANCO, size=10, name="Calibri")
        cell.fill      = PatternFill("solid", fgColor=bg_resumen)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 20

        # ── INFO: nodos de la planilla que están en otro proyecto del mismo LCL ──
        if fuera_alcance:
            padres_txt = ", ".join(dict.fromkeys(
                p["padre"] for _nd, ps in fuera_alcance for p in ps if p["padre"]))
            nodos_txt = ", ".join(
                f"{nd} ({' + '.join(p['code'] for p in ps)})" for nd, ps in fuera_alcance)
            r += 2
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
            cell = ws.cell(row=r, column=1, value=(
                f"INFO — {len(fuera_alcance)} nodo(s) de la planilla estan en otro proyecto "
                f"SGS del mismo LCL y no se cruzaron: {nodos_txt}. Para cruzarlos, consulta "
                f"con el PRO padre" + (f" ({padres_txt})" if padres_txt else "") + "."))
            cell.font      = Font(size=9, color="1F3864", name="Calibri", bold=True)
            cell.fill      = PatternFill("solid", fgColor=C_AZUL_C)
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            ws.row_dimensions[r].height = 42

        # ── Alerta: materiales SIP sin par en SGS (regla de negocio) ──────────
        # Puede haber materiales en SGS que no estén en SIP (normal), pero NO al
        # revés: un material SIP que no esté en SGS es una violación que se alerta.
        if alertas_sip or alertas_nodo:
            criticos = [a for a in alertas_sip if a[0] == "CRITICO"]
            revisar  = [a for a in alertas_sip if a[0] == "REVISAR"]
            r += 2
            titulo = (
                f"ALERTA — Materiales SIPWEBORA sin par en SGS: "
                f"{len(criticos)} CRITICO(S) (ausentes de todo SGS), "
                f"{len(revisar)} por revisar (existen en SGS pero en otro nodo)"
                + (f", {len(alertas_nodo)} nodo(s) SIP que no existen en el proyecto SGS"
                   if alertas_nodo else "")
                + ".  Regla: no pueden existir materiales en SIP que no esten en SGS."
            )
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
            cell = ws.cell(row=r, column=1, value=titulo)
            cell.font      = Font(bold=True, color=BLANCO, size=10, name="Calibri")
            cell.fill      = PatternFill("solid",
                                         fgColor=C_ROJO if (criticos or alertas_nodo) else C_AMBAR)
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            ws.row_dimensions[r].height = 30
            r += 1
            pro_padre = sgs_data["padre"].get("code", "")
            for nodo, n_mat, fuera in alertas_nodo:
                if nodo == "—":
                    msg = (f"  CRITICO — {n_mat} material(es) SIP sin nodo en la planilla: "
                           f"no se pueden cruzar con SGS.")
                elif fuera:
                    donde = "; ".join(
                        f"{f['code']} «{f['name']}»"
                        + (f" cuelga de {f['padre']}" if f["padre"] else " no tiene padre")
                        for f in fuera)
                    msg = (f"  CRITICO — Nodo {nodo} ({n_mat} material(es) SIP) no esta en "
                           f"{pro_padre}. En SGS esta como {donde}, fuera de los proyectos "
                           f"de este cruce: revisar de que proyecto debe colgar.")
                else:
                    msg = (f"  CRITICO — Nodo {nodo} ({n_mat} material(es) SIP) no se encontro "
                           f"en el proyecto SGS consultado ({pro_padre}).")
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
                c = ws.cell(row=r, column=1, value=msg)
                c.font      = Font(size=9, color=BLANCO, name="Calibri")
                c.fill      = PatternFill("solid", fgColor=C_ROJO)
                c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                ws.row_dimensions[r].height = 28 if fuera else 14
                r += 1
            raros = sgs_data.get("_nodos_raros") or []
            if alertas_nodo and raros:
                lista = "; ".join(f"{x['code']} «{x['name']}» (nodo {x['clave']})" for x in raros[:8])
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
                c = ws.cell(row=r, column=1, value=(
                    f"  REVISAR — En SGS hay {len(raros)} nodo(s) con material cuyo nombre no se lee "
                    f"como numero de nodo: {lista}{' ...' if len(raros) > 8 else ''}. Pueden ser los "
                    f"nodos SIP de arriba escritos de otra forma en SGS."))
                c.font      = Font(size=9, color="7B3F00", name="Calibri")
                c.fill      = PatternFill("solid", fgColor=C_AMBAR)
                c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                ws.row_dimensions[r].height = 28
                r += 1
            for sev, nodo, cod, desc, cant, otros in criticos + revisar:
                if sev == "CRITICO":
                    msg = (f"  CRITICO — {cod} ({str(desc)[:50]}) en SIP nodo {nodo}, "
                           f"cant {self._fmt_qty(cant)}: NO existe en ningun nodo de SGS.")
                    bg, fg = C_ROJO, BLANCO
                else:
                    msg = (f"  REVISAR — {cod} ({str(desc)[:50]}) esta en SIP nodo {nodo} "
                           f"pero en SGS aparece en nodo(s): {', '.join(otros) or '—'}.")
                    bg, fg = C_AMBAR, "7B3F00"
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
                c = ws.cell(row=r, column=1, value=msg)
                c.font      = Font(size=9, color=fg, name="Calibri")
                c.fill      = PatternFill("solid", fgColor=bg)
                c.alignment = Alignment(horizontal="left", vertical="center")
                ws.row_dimensions[r].height = 14
                r += 1

        # ── tramos con los mismos materiales y otro nombre ───────────────
        eqs = sgs_data.get("_equivalencias") or []
        if eqs:
            n_pend = sum(1 for p in eqs if p["estado"] == "pendiente")
            r += 2
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
            cell = ws.cell(row=r, column=1, value=(
                f"EQUIVALENCIAS — {len(eqs)} nodo(s) SIP sin nodo SGS del mismo nombre tienen un nodo SGS "
                f"con los mismos materiales ({n_pend} por decidir). La facturadora decide en la "
                "previsualización del Balance si es el mismo tramo; mientras no se apruebe, sigue en rojo."))
            cell.font      = Font(bold=True, color="000000", size=10, name="Calibri")
            cell.fill      = PatternFill("solid", fgColor=C_EQUIV)
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            ws.row_dimensions[r].height = 30
            r += 1
            for p in eqs:
                amb = f" También se parece a {', '.join(p['ambigua'])}." if p.get("ambigua") else ""
                cons = " En SGS ya está consumido." if p.get("consumido_total") else ""
                if p["estado"] == "aprobada" and not p.get("ambigua"):
                    msg = (f"  EQUIVALENCIA APROBADA — SIP {p['sip']} se cruza como SGS {p['sgs']} ({p['pro']}): "
                           f"{p['resumen']}. Aprobó {p.get('por') or '—'} el {p.get('en') or '—'}.{cons}")
                elif p["estado"] == "rechazada":
                    msg = (f"  EQUIVALENCIA RECHAZADA — SIP {p['sip']} no es SGS {p['sgs']} ({p['pro']}). "
                           f"Decidió {p.get('por') or '—'} el {p.get('en') or '—'}.")
                else:
                    msg = (f"  EQUIVALENCIA — SIP {p['sip']} ≈ SGS {p['sgs']} ({p['pro']}): {p['resumen']}. "
                           f"Si es el mismo tramo, apruébalo en la previsualización del Balance.{cons}{amb}")
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
                c = ws.cell(row=r, column=1, value=msg)
                c.font      = Font(size=9, color="000000", name="Calibri")
                c.fill      = PatternFill("solid", fgColor=C_VERDE_SGS if msg.startswith("  EQUIVALENCIA APROBADA")
                                          else (C_PAR if "RECHAZADA" in msg else C_EQUIV))
                c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                ws.row_dimensions[r].height = 28
                r += 1

        # ── Alerta: filas repetidas dentro de una planilla SIP (ver CHANGELOG 2026-10-19) ──────────
        # Solo en nodos que salen con diferencia: dos filas iguales en un nodo que
        # cuadra pueden ser legítimas y no hay nada que revisar.
        duplicados = [(pl, nd, cods) for pl, nd, cods
                      in sip_datos.get("metadata", {}).get("_duplicados") or []
                      if _canon_cable(nd) in nodos_dif]
        if duplicados:
            r += 2
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
            cell = ws.cell(row=r, column=1, value=(
                "ALERTA — Filas repetidas en la planilla SIPWEBORA (mismo nodo, codigo y "
                "tipo) en nodos con diferencia. El cruce las suma: si se cargaron dos "
                "veces, de ahi puede salir la diferencia con SGS."))
            cell.font      = Font(bold=True, color="7B3F00", size=10, name="Calibri")
            cell.fill      = PatternFill("solid", fgColor=C_AMBAR)
            cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
            ws.row_dimensions[r].height = 30
            r += 1
            for pl, nodo, cods in duplicados:
                lista = ", ".join(cods[:8]) + (", ..." if len(cods) > 8 else "")
                ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
                c = ws.cell(row=r, column=1, value=(
                    f"  REVISAR — Planilla {pl}, nodo {nodo}: {len(cods)} material(es) "
                    f"aparecen dos veces ({lista})."))
                c.font      = Font(size=9, color="7B3F00", name="Calibri")
                c.fill      = PatternFill("solid", fgColor=C_AMBAR)
                c.alignment = Alignment(horizontal="left", vertical="center")
                ws.row_dimensions[r].height = 14
                r += 1

        ws.freeze_panes = ws["A10"]   # una fila más de leyenda (C)

    # ── Hoja 5: CRUCE_LIQUIDADOR (liquidador) ─────────────────────────────────────

    @staticmethod
    def _sgs_dedup_nodo(nodo: dict) -> list:
        """
        Retorna entradas SGS únicas (una por material) en orden liq_materiales.
        Usado por _hoja_cruce_facturas y _hoja_sgs_balance como índice auxiliar.
        """
        sgs_idx = _sgs_idx_nodo(nodo)
        seen_doms = set()
        result = []
        for entry in sgs_idx.values():
            dom = entry["dom"]
            if dom in seen_doms:
                continue
            seen_doms.add(dom)
            canonical = entry["cdc"] or entry["alt"] or dom
            cod_base = canonical[:-1] if (canonical.endswith("C") and len(canonical) > 1) else canonical
            result.append(dict(entry, cod_base=cod_base))
        return result

    @staticmethod
    def _sgs_ordenado_desde_bandejas(nodo: dict) -> list:
        """
        Retorna TODOS los materiales SGS del nodo en orden de bandejas.

        Orden:
          1. Bandejas tipo 'deliver' ordenadas por nombre (BAN-097008 < BAN-097086...)
             — en el orden en que aparecen sus materiales dentro de cada bandeja.
          2. Al final: materiales en liq_materiales que no aparecen en ninguna bandeja
             (ej: materiales proyectados pero nunca despachados).

        Incluye materiales con neto=0 (devueltos íntegramente, o qty=0 en bandeja).
        No filtra por cantidad — el usuario necesita el inventario completo.

        Cada entrada del resultado:
          {dom, alt, cdc, desc, neto, consumido, pendiente, es_c, cod_base, ban}
          donde 'ban' es el nombre de la bandeja deliver de origen (o '' si solo liq).
        """
        # ── Neto por DOM desde bandejas crudas ───────────────────────────────
        ban_agg = _agg_bandejas(nodo)  # {dom: {despachado, devuelto, ...}}

        # ── Índice liq_materiales por DOM ────────────────────────────────────
        liq_idx = {}
        for m in nodo.get("liq_materiales", []):
            dom, alt, desc = parsear_producto(m.get("product_id"))
            cdc = (m.get("customer_default_code") or "").strip()
            if dom:
                liq_idx[dom] = {
                    "alt": alt, "cdc": cdc, "desc": desc,
                    "advance": float(m.get("advance", 0) or 0),
                }

        # ── Orden: iterar bandejas deliver ordenadas por nombre ───────────────
        deliver_bans = sorted(
            [b for b in nodo.get("bandejas", []) if b.get("type", "deliver") != "devolution"],
            key=lambda b: b.get("name", ""),
        )

        seen_doms = []   # lista ordenada sin duplicados
        dom_to_ban = {}  # DOM → nombre de la primera bandeja deliver que lo contiene

        for ban in deliver_bans:
            ban_name = ban.get("name", "")
            for m in ban.get("materiales", []):
                dom, _, _ = parsear_producto(m.get("product_id"))
                if dom and dom not in seen_doms:
                    seen_doms.append(dom)
                    dom_to_ban[dom] = ban_name

        # Agregar liq_materiales que no estaban en ninguna bandeja deliver
        for dom in liq_idx:
            if dom not in seen_doms:
                seen_doms.append(dom)

        # ── Construir resultado ───────────────────────────────────────────────
        result = []
        for dom in seen_doms:
            agg  = ban_agg.get(dom, {"despachado": 0.0, "devuelto": 0.0})
            neto = agg["despachado"] - agg["devuelto"]
            liq  = liq_idx.get(dom, {"alt": "", "cdc": "", "desc": dom, "advance": 0.0})

            cdc  = liq["cdc"]
            alt  = liq["alt"]
            desc = liq["desc"]
            consumido = liq["advance"]

            canonical = cdc or alt or dom
            es_c      = canonical.endswith("C") and len(canonical) > 1
            cod_base  = canonical[:-1] if es_c else canonical

            result.append({
                "dom":       dom,
                "alt":       alt,
                "cdc":       cdc,
                "desc":      desc,
                "neto":      neto,
                "consumido": consumido,
                "pendiente": neto - consumido,
                "es_c":      es_c,
                "cod_base":  cod_base,
                "ban":       dom_to_ban.get(dom, ""),  # bandeja de origen
            })

        return result

    def _hoja_cruce_sergio(self, wb, sip_datos, sgs_data):
        """
        Vista exclusiva del liquidador.

        Reglas:
          · Orden: bandejas deliver (BAN-XXXXX) de menor a mayor, luego liq_materiales
            sin bandeja (proyectados no despachados).
          · Muestra TODOS los materiales SGS aunque neto=0.
          · 170846 y 170846C equivalentes — normalizar al código base (sin C).
          · Match SIP por código base; fallback por descripción normalizada.
          · Chatarra confirmada (SIP=chatarra Y SGS neto=0) → EXCLUIR.
          · Verde    → SIP qty == SGS neto  (cuadra)
          · Rojo     → SIP qty != SGS neto  (descuadre)
          · Ámbar    → Solo SGS (sin match SIP)
          · Gris     → SIN MOV (neto=0 y sin SIP)
        """
        # A   B      C       D             E          F        G
        # #   Nodo   Código  Descripción   SIP Cant   SGS Neto Estado
        NCOLS = 7
        HDR = ["#", "Nodo", "Código", "Descripción",
               "SIP Cant.", "SGS Neto", "Estado"]

        ws = wb.create_sheet("CRUCE_LIQUIDADOR")
        ws.sheet_view.showGridLines = False
        for i, w in enumerate([5, 8, 14, 50, 12, 12, 14], 1):
            ws.column_dimensions[get_column_letter(i)].width = w

        r = _titulo_hoja(
            ws, 1,
            "CRUCE SGS -> SIP -- Liquidador",
            C_HDR_SERG, NCOLS,
        )

        C_GRIS = "D9D9D9"  # gris — sin movimiento (neto=0, sin SIP)

        # Leyenda compacta
        for bg, txt in [
            (C_VERDE,        "Verde  -- Cantidades SGS y SIP coinciden"),
            (C_ROJO,         "Rojo   -- NO CUADRA: SIP sin par en SGS, SGS despachado sin par en SIP, o cantidades distintas"),
            (C_GRIS,         "Gris   -- Sin movimiento neto (devuelto o qty=0)"),
        ]:
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
            cell = ws.cell(row=r, column=1, value=f"  {txt}")
            cell.font      = Font(size=8, italic=True, name="Calibri", color="000000")
            cell.fill      = PatternFill("solid", fgColor=bg)
            cell.alignment = Alignment(horizontal="left", vertical="center")
            ws.row_dimensions[r].height = 13
            r += 1
        r += 1

        # Cabecera
        for col_i, h in enumerate(HDR, 1):
            _c(ws, r, col_i, h, bold=True, bg=C_SUBHDR, align_h="center", size=9)
        ws.row_dimensions[r].height = 14
        r += 1

        # ── Índice SIP: todos los materiales — clave (nodo, cod_base) ─────
        sip_idx_all = self._sip_idx_cruce(sip_datos)   # con equivalencias aprobadas
        sip_idx = {}
        for (nodo, cod), v in sip_idx_all.items():
            cod_base = cod[:-1] if (cod.endswith("C") and len(cod) > 1) else cod
            key = (nodo, cod_base)
            if key in sip_idx:
                sip_idx[key]["cantidad"] = float(sip_idx[key]["cantidad"]) + float(v["cantidad"])
            else:
                sip_idx[key] = dict(v)

        tot_ok = tot_rojo = tot_solo = tot_sinmov = 0
        idx_global = 1

        for nodo in sgs_data["nodos"]:
            nodo_sip_key = _nodo_key_sgs(nodo)
            nodo_str     = nodo_sip_key  # etiqueta: refleja el cable real (p.ej. 4AL23)

            # SGS: todos los materiales en orden de bandejas, incluyendo neto=0.
            # Un nodo SGS vacío ya no se salta (ver CHANGELOG 2026-10-19): sus filas SIP salen como
            # SOLO SIP, igual que en CRUCE_FACTURAS; sin filas, se salta más abajo.
            sgs_entries = self._sgs_ordenado_desde_bandejas(nodo)

            # Agrupar entradas con mismo código base (140991 y 140991C son el mismo)
            # cod_base se normaliza (sin "C") para lookup SIP.
            # cod_display refleja el código SGS activo (el que tiene neto > 0).
            # Si ambos tienen neto > 0 (edge case), se suman y se usa el base.
            _sgs_groups = {}   # cb_norm → [entries]
            _sgs_order  = []
            for _e in sgs_entries:
                _cb_raw  = _e["cod_base"]
                _cb_norm = _cb_raw[:-1] if (_cb_raw.endswith("C") and len(_cb_raw) > 1) else _cb_raw
                if _cb_norm not in _sgs_groups:
                    _sgs_groups[_cb_norm] = []
                    _sgs_order.append(_cb_norm)
                _sgs_groups[_cb_norm].append(_e)

            sgs_entries = []
            for _cb_norm in _sgs_order:
                _group = _sgs_groups[_cb_norm]
                if len(_group) == 1:
                    _entry = dict(_group[0])
                    _entry["cod_base"]    = _cb_norm   # normalizado para SIP lookup
                    # cod_display: código real SGS (cdc tiene el valor con o sin C)
                    _entry["cod_display"] = _group[0].get("cdc") or _group[0].get("alt") or _cb_norm
                else:
                    # Dos variantes: priorizar el que tiene neto > 0 para display
                    _total  = sum(float(_e["neto"]) for _e in _group)
                    _active = next((_e for _e in _group if float(_e["neto"]) > 0), _group[0])
                    _entry  = dict(_active)
                    _entry["cod_base"]    = _cb_norm   # normalizado para SIP lookup
                    _entry["cod_display"] = _active.get("cdc") or _active.get("alt") or _cb_norm
                    _entry["neto"]        = _total
                sgs_entries.append(_entry)

            # SIP de este nodo: {cod_base: entry}
            sip_nodo = {}
            for (nd, cb), v in sip_idx.items():
                if nd == nodo_sip_key:
                    sip_nodo[cb] = v

            # Índice SIP por descripción: {desc_norm: (cod_base, entry)}
            sip_by_desc = {}
            for cb, v in sip_nodo.items():
                dn = _normalizar_desc(v.get("desc", ""))
                if dn:
                    sip_by_desc.setdefault(dn, (cb, v))

            filas_nodo  = []
            sip_usados  = set()   # cb de SIP que sí encontraron par en SGS

            for sgs_e in sgs_entries:
                cb       = sgs_e["cod_base"]               # normalizado, para lookup SIP
                cb_disp  = sgs_e.get("cod_display", cb)    # código activo SGS, para mostrar
                sgs_neto = float(sgs_e["neto"])
                desc     = sgs_e.get("desc") or cb_disp
                ban_nom  = sgs_e.get("ban", "")

                # Match SIP: 1) por código base, 2) por descripción normalizada
                sip_v = sip_nodo.get(cb)
                if sip_v is not None:
                    sip_usados.add(cb)
                else:
                    dn_sgs = _normalizar_desc(desc)
                    match  = sip_by_desc.get(dn_sgs)
                    if match:
                        sip_usados.add(match[0])
                        _, sip_v = match

                if sip_v is not None:
                    sip_cant = float(sip_v["cantidad"])
                    sip_tipo = sip_v.get("tipo", "")
                    desc_display = desc or sip_v.get("desc") or cb_disp

                    # Chatarra confirmada (SIP=chatarra, SGS neto=0) → excluir
                    if sip_tipo in CHATARRA_TIPOS and sgs_neto == 0:
                        continue

                    if abs(sip_cant - sgs_neto) < 0.001:
                        estado, bg_row = "OK", C_VERDE
                        tot_ok += 1
                    else:
                        dif    = sip_cant - sgs_neto
                        estado, bg_row = f"{dif:+.2f}", C_ROJO
                        tot_rojo += 1

                    filas_nodo.append({
                        "ban": ban_nom, "base": cb_disp, "desc": desc_display,
                        "sip": sip_cant, "sgs": sgs_neto,
                        "estado": estado, "bg": bg_row,
                    })

                else:
                    # Sin match SIP. Si SGS lo despachó (neto>0) y no está en SIP
                    # → ROJO (regla). Si neto=0 (no se movió) → gris.
                    if sgs_neto == 0:
                        estado, bg_row = "SIN MOV", C_GRIS
                        tot_sinmov += 1
                    else:
                        estado, bg_row = "SOLO SGS (DESPACHADO, NO EN SIP)", C_ROJO
                        tot_rojo += 1

                    filas_nodo.append({
                        "ban": ban_nom, "base": cb_disp, "desc": desc,
                        "sip": None, "sgs": sgs_neto,
                        "estado": estado, "bg": bg_row,
                    })

            # SOLO SIP: lo que SIP instaló en este nodo y NO tiene par en SGS.
            # REGLA: todo material instalado en SIP debe existir en SGS → ROJO.
            for cb, v in sip_nodo.items():
                if cb in sip_usados or v.get("tipo", "") in CHATARRA_TIPOS:
                    continue   # chatarra (970/ I RZ…) no va en SGS
                filas_nodo.append({
                    "ban": "", "base": cb, "desc": v.get("desc", "") or cb,
                    "sip": float(v["cantidad"]), "sgs": None,
                    "estado": "SOLO SIP (NO EN SGS)", "bg": C_ROJO,
                })
                tot_rojo += 1

            if not filas_nodo:
                continue

            pro_code = nodo.get("code", "")
            _sep_lbl = f"  Nodo: {nodo_str}  PRO: {pro_code}" if pro_code else f"  Nodo: {nodo_str}"
            r = _separador_nodo(ws, r, _sep_lbl, C_HDR_SERG, NCOLS)

            for f in filas_nodo:
                _c(ws, r, 1, idx_global,                                    bg=BLANCO,  align_h="center", size=9)
                _c(ws, r, 2, nodo_str,                                      bg=BLANCO,  align_h="center", size=9)
                _c(ws, r, 3, f["base"],                                     bg=BLANCO,  align_h="center", size=9)
                _c(ws, r, 4, f["desc"],                                     bg=BLANCO,  align_h="left",   size=9, wrap=True)
                _c(ws, r, 5,
                   self._fmt_qty(f["sip"]) if f["sip"] is not None else "—",
                   bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 6, self._fmt_qty(f["sgs"]),                      bg=f["bg"], align_h="center", size=9)
                _c(ws, r, 7, f["estado"],                                   bg=f["bg"], align_h="center", size=9, bold=True)
                ws.row_dimensions[r].height = 14
                r += 1
                idx_global += 1

        # Nodos SIP que no existen en el proyecto SGS (ver CHANGELOG 2026-10-19): mismo bloque que en
        # CRUCE_FACTURAS, sin chatarra. El separador va SIN «PRO:» (no hay proyecto
        # donde liquidar) para que ningún lector del Excel tome un PRO equivocado.
        # Fuera: los que están en otro proyecto del mismo LCL (no consultado) y las
        # filas SIP sin nodo, que el Liquidador no puede ubicar.
        claves_sgs  = {_nodo_key_sgs(_n) for _n in sgs_data["nodos"]}
        nodos_fuera = sgs_data.get("_nodos_fuera") or {}
        for nodo_str in sorted({nd for (nd, _cb) in sip_idx} - claves_sgs, key=_nodo_sort_key):
            if nodo_str == "—" or (nodos_fuera.get(nodo_str) or {}).get("alcance"):
                continue
            filas_nodo = [
                {"base": cb, "desc": v.get("desc", "") or cb, "sip": float(v["cantidad"])}
                for (nd, cb), v in sorted(sip_idx.items())
                if nd == nodo_str and v.get("tipo", "") not in CHATARRA_TIPOS
            ]
            if not filas_nodo:
                continue
            # sin «PRO:» en el texto (el Liquidador no debe tomarlo como destino)
            _eqp = next((p for p in sgs_data.get("_equivalencias") or []
                         if p["sip"] == nodo_str and p["estado"] == "pendiente"), None)
            r = _separador_nodo(ws, r, f"  Nodo: {nodo_str}  —  NO EXISTE EN SGS"
                                + (f"  ·  ≈ SGS {_eqp['sgs']} pendiente de aprobar" if _eqp else ""),
                                C_HDR_SERG, NCOLS)
            for f in filas_nodo:
                _c(ws, r, 1, idx_global, bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 2, nodo_str,   bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 3, f["base"],  bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 4, f["desc"],  bg=BLANCO, align_h="left",   size=9, wrap=True)
                _c(ws, r, 5, self._fmt_qty(f["sip"]), bg=BLANCO, align_h="center", size=9)
                _c(ws, r, 6, "—", bg=C_ROJO, align_h="center", size=9)
                _c(ws, r, 7, "SOLO SIP (NO EN SGS)", bg=C_ROJO, align_h="center", size=9, bold=True)
                ws.row_dimensions[r].height = 14
                r += 1
                idx_global += 1
                tot_rojo += 1

        # Resumen global
        r += 1
        bg_res = C_VERDE if tot_rojo == 0 else C_ROJO
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NCOLS)
        cell = ws.cell(row=r, column=1,
                       value=(f"RESUMEN -- OK: {tot_ok}  |  Descuadre: {tot_rojo}  |  "
                              f"Solo SGS: {tot_solo}  |  Sin mov: {tot_sinmov}"))
        cell.font      = Font(bold=True, color=BLANCO, size=10, name="Calibri")
        cell.fill      = PatternFill("solid", fgColor=bg_res)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[r].height = 20

        ws.freeze_panes = ws["A9"]

    # ── Helpers de índice SIP ─────────────────────────────────────────────────

    @staticmethod
    def _filtrar_nodos_por_sip(sip_datos: dict, sgs_data: dict) -> dict:
        """
        Recorta los nodos SGS a solo los que existen en la planilla SIP.

        El proyecto padre SGS arrastra todos sus nodos (PRO-XXXX), pero el balance
        integrado solo compara los nodos que SIP reporta. Se normalizan ambos lados
        con la misma función que usa el cruce de facturas para que coincidan formatos
        ('NODO 1A' ↔ '1A', '1 al 2' ↔ '1AL2').

        Si ningún nodo SGS coincide (posible problema de normalización), se devuelven
        todos los nodos sin filtrar para no entregar un balance vacío.
        """
        nodos_sgs = sgs_data.get("nodos", [])
        if not nodos_sgs:
            return sgs_data

        # Conjunto de nodos SIP (forma cruda + normalizada).
        sip_nodos = set()
        for item in sip_datos.get("mat", []):
            nd = (item.get("nodo") or "").strip()
            if not nd:
                continue
            sip_nodos.add(nd)
            norm = _normalizar_nodo_sip(nd)
            if norm:
                sip_nodos.add(norm)
                sip_nodos.add(_canon_cable(norm))

        if not sip_nodos:
            return sgs_data

        def _coincide(n: dict) -> bool:
            key = _nodo_key_sgs(n)
            raw = str(n.get("nodo") or n.get("name", ""))
            return key in sip_nodos or raw in sip_nodos

        filtrados = [n for n in nodos_sgs if _coincide(n)]
        if not filtrados:
            return sgs_data  # sin coincidencias → no recortar (evita balance vacío)

        return dict(sgs_data, nodos=filtrados)

    @staticmethod
    def _merge_liq_materiales(a: list, b: list) -> list:
        """
        Une dos listas de project.material sumando los duplicados por producto.
        Necesario al fusionar nodos: _sgs_idx_nodo indexa por código y una
        segunda fila del mismo producto sobreescribiría el 'advance' de la
        primera en lugar de sumarlo.
        """
        def _pid(m):
            p = m.get("product_id")
            return p[0] if isinstance(p, list) else p

        out = [dict(m) for m in a]
        idx = {_pid(m): m for m in out if _pid(m) is not None}
        for m in b:
            pid = _pid(m)
            ex  = idx.get(pid)
            if ex is None:
                nuevo = dict(m)
                out.append(nuevo)
                if pid is not None:
                    idx[pid] = nuevo
                continue
            for campo in ("advance", "amount", "tray", "despachado",
                          "balance", "diferencia"):
                try:
                    ex[campo] = float(ex.get(campo) or 0) + float(m.get(campo) or 0)
                except (TypeError, ValueError):
                    pass
            if not ex.get("customer_default_code") and m.get("customer_default_code"):
                ex["customer_default_code"] = m["customer_default_code"]
        return out

    @staticmethod
    def _merge_nodos_duplicados(nodos: list) -> list:
        """
        Fusiona proyectos SGS que representan el MISMO nodo físico (misma clave
        normalizada por _nodo_key_sgs). Concatena bandejas y liq_detalles, suma
        los liq_materiales duplicados por producto y une los códigos PRO en la
        etiqueta ('PRO-053400 + PRO-053170'). El orden de nodos se conserva.
        """
        # El PRO con despacho va primero en «PRO: A + B» (ver CHANGELOG 2026-10-19): quien lea ese rótulo
        # (CRUCE_LIQUIDADOR, respaldo de Liquidador 2) toma A. SGS_BALANCE ya no funde:
        # va proyecto por proyecto (ver preparar_cruce).
        grupos, orden = {}, []
        for n in nodos:
            key = _nodo_key_sgs(n)
            if key not in grupos:
                grupos[key] = []
                orden.append(key)
            grupos[key].append(n)
        def _movimiento(x):
            # 0 = tiene bandejas (despacho real), 1 = liquidación con cantidades,
            # 2 = solo proyectado o vacío. Lo proyectado no cuenta como material.
            if x.get("bandejas"):
                return 0
            if any(float(m.get("despachado") or 0) or float(m.get("advance") or 0)
                   for m in x.get("liq_materiales", [])):
                return 1
            return 2
        nodos = [n for key in orden for n in sorted(grupos[key], key=_movimiento)]

        por_key = {}
        orden   = []
        for n in nodos:
            key = _nodo_key_sgs(n)
            base = por_key.get(key)
            if base is None:
                por_key[key] = dict(n)
                orden.append(key)
                continue
            base["bandejas"]     = list(base.get("bandejas", []))     + list(n.get("bandejas", []))
            base["liq_detalles"] = list(base.get("liq_detalles", [])) + list(n.get("liq_detalles", []))
            base["liq_materiales"] = BalanceIntegrado._merge_liq_materiales(
                base.get("liq_materiales", []), n.get("liq_materiales", [])
            )
            codes = [c for c in (base.get("code"), n.get("code")) if c]
            base["code"] = " + ".join(dict.fromkeys(codes))
        return [por_key[k] for k in orden]

    @staticmethod
    def _sip_mat_idx(mat_list: list) -> dict:
        """
        Construye índice de materiales SIPWEBORA.
        Retorna {(nodo, codigo): {cantidad, tipo, desc}}
        """
        tipo_map = {"i_n": "I N", "i_rz": "I RZ", "r_ch": "R CH",
                    "r_rz": "R RZ", "m_a": "M A"}
        idx = {}
        for item in mat_list:
            nodo = _canon_cable(item["nodo"]) if item["nodo"] else "—"
            cod  = item["codigo"]
            tipo_key = next(
                (k for k in ["i_n", "i_rz", "r_ch", "r_rz", "m_a"] if item[k] != ""), None
            )
            cant = item[tipo_key] if tipo_key else 0.0
            tipo = tipo_map.get(tipo_key, "")
            if isinstance(cant, (int, float)) and cant == int(cant):
                cant = int(cant)
            key = (nodo, cod)
            if key in idx:
                idx[key]["cantidad"] = float(idx[key]["cantidad"]) + float(cant)
            else:
                idx[key] = {"cantidad": cant, "tipo": tipo, "desc": item["desc"]}
        return idx

    @staticmethod
    def _sgs_raw_keys(sgs_idx: dict) -> set:
        """Retorna el conjunto de claves SGS primarias (sin los alias de normalización C)."""
        # Las claves "cdc o alt" (el valor "sip_key" original antes de aliases)
        return {
            v["cdc"] or v["alt"] or v["dom"]
            for v in sgs_idx.values()
        }

    @staticmethod
    def _fmt_qty(v):
        """Formatea cantidad: entero si es entero, float con 2 decimales si no."""
        if v is None:
            return "—"
        if isinstance(v, float) and v == int(v):
            return int(v)
        return round(float(v), 2)


# ── Utilidades ─────────────────────────────────────────────────────────────────

def _nodo_sort_key(n):
    """Ordena nodos numéricamente."""
    m = re.match(r'^0*(\d+)([A-Za-z]*)$', str(n))
    if m:
        return (int(m.group(1)), m.group(2).upper())
    return (float("inf"), str(n))
