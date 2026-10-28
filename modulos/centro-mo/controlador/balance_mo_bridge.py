# -*- coding: utf-8 -*-
"""
balance_mo_bridge.py — Puente Correo (Outlook) → Balance MO (nuevo/, Balance ERP)

Detecta en Outlook los correos del MES ACTUAL marcados con bandera roja o con la
categoría "Orden Pendiente", clasifica el proyecto como MI (código DF...) o
Programados (código LCL...), y si hay un Excel adjunto con la tabla de Mano de
Obra (formato SIP_MANO_OBRA: Nodo/Código QP/Cantidad + metadatos Ticket-PDL/
ODM-Grafo/LCL) la extrae.

Con esos datos arma una SESIÓN pre-llenada en el wizard "Balance SGS Puro
manual" del proyecto nuevo/ (módulo Balance),
importando sus propios módulos como librería — nunca se modifica ni un archivo
de ese proyecto, y nunca se toca materiales ni se escribe nada en SGS/Odoo.
El operador revisa y confirma líderes/% a mano en /manual/paso2/<sid>, igual que
si hubiera tecleado el código en el wizard.

Requiere que nuevo/ esté disponible localmente en la ruta de abajo (mismo PC).
"""
import os
import re
import sys
import time
import uuid
import zipfile
import tempfile
import unicodedata
from datetime import datetime
from xml.etree import ElementTree

# COM opcional: en el servidor SGO7 (Linux) no hay pywin32 y las funciones de Outlook de este
# archivo no se usan allá (las corre el agente del PC, agente_centro_mo.py).
try:
    import pythoncom
    import win32com.client
except ImportError:          # pragma: no cover
    pythoncom = None
    win32com = None
import openpyxl

# ── Ruta al proyecto nuevo/ (Balance) ─────────────────────────────────────────
# Orden: variable NUEVO_DIR → carpeta hermana ../sony-sgs/nuevo del repositorio. Si ninguna
# existe, se devuelve la relativa para que el error diga claramente qué carpeta falta.
def _resolver_nuevo_dir() -> str:
    relativa = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sony-sgs", "nuevo"))
    candidatos = [
        os.environ.get("NUEVO_DIR", ""),
        relativa,
    ]
    for c in candidatos:
        if c and os.path.isdir(c):
            return c
    return relativa


NUEVO_DIR = _resolver_nuevo_dir()
NUEVO_PUERTO = 5002

if NUEVO_DIR not in sys.path:
    sys.path.insert(0, NUEVO_DIR)

OL_MAIL = 43

CARPETAS_IGNORADAS = {
    "calendario", "calendar", "contactos", "contacts", "tareas", "tasks",
    "notas", "notes", "diario", "journal", "fuentes rss", "rss feeds",
    "sincronización", "sync issues", "problemas de sincronización",
    "correo no deseado", "junk email", "junk e-mail",
}

# Rangos acotados a propósito (visto en datos reales: DF siempre trae 12
# dígitos, alguna vez 11 truncado; LCL siempre 10). Sin tope, \d+ puede
# fusionar dos números vecinos en el texto plano de un correo reenviado
# (ej. "...DF20266454583709..." = dos códigos pegados) en un solo falso
# código. Se admite un separador opcional porque el asunto real trae formas
# como "LCL 6300996351" (con espacio).
# Lookarounds en vez de \b: el guion bajo es "carácter de palabra", así que
# \b NO ve frontera en "DF202664588119_CAMBIO" (formato real de asuntos) y el
# código se perdía. (?<![A-Z0-9]) evita pegarse a otro código; (?!\d) evita
# cortar un número más largo.
RE_DF = re.compile(r"(?<![A-Za-z0-9])DF[\s_-]?(\d{10,13})(?!\d)", re.IGNORECASE)
RE_LCL = re.compile(r"(?<![A-Za-z0-9])LCL[\s_-]?(\d{8,12})(?!\d)", re.IGNORECASE)
# LCL "pelada": en tablas HTML el rótulo LCL queda en la fila de encabezado y
# el número solo en la fila de datos. Todas las LCL reales son 10 dígitos que
# empiezan por 630 — se detectan sueltas y el árbitro SGS descarta las falsas
# (Factibilidad 15..., AGP 20..., PROT 10... no calzan el prefijo).
RE_LCL_PELADA = re.compile(r"(?<![0-9])(630\d{7})(?!\d)")


def _normalizar_match(m, prefijo):
    return f"{prefijo}{m.group(1)}"


def url_paso2(sid: str) -> str:
    # Entrar por /manual/retomar y no por /manual/paso2 directo: retomar relanza
    # el fetch de SGS dentro del proceso del servidor y redirige a paso2. Si el
    # link fuera a paso2 directo, una sesión creada por OTRO proceso (este
    # puente) podría quedar "cargando SGS" para siempre cuando el hilo de fetch
    # del proceso creador ya no existe.
    return f"http://localhost:{NUEVO_PUERTO}/manual/retomar/{sid}"


# ---------------------------------------------------------------- COM helpers

def _carpetas_de_correo(folder, ruta=""):
    ruta_actual = f"{ruta}/{folder.Name}" if ruta else folder.Name
    if folder.Name.strip().lower() not in CARPETAS_IGNORADAS:
        try:
            if folder.DefaultItemType == 0:
                yield ruta_actual, folder
        except Exception:
            pass
        try:
            subs = list(folder.Folders)
        except Exception:
            subs = []
        for sub in subs:
            yield from _carpetas_de_correo(sub, ruta_actual)


def _clasificar(codigo: str) -> str:
    c = (codigo or "").upper()
    if c.startswith("DF"):
        return "MI"
    if c.startswith("LCL"):
        return "Programados"
    return ""


def _detectar_codigo(texto: str, fuente: str):
    """Busca DF (MI) primero, luego LCL (Programados). Retorna (codigo, tipo, fuente)."""
    if not texto:
        return "", "", ""
    m = RE_DF.search(texto)
    if m:
        return _normalizar_match(m, "DF"), "MI", fuente
    m = RE_LCL.search(texto)
    if m:
        return _normalizar_match(m, "LCL"), "Programados", fuente
    return "", "", ""


# ------------------------------------------------------- parser de Excel (MO)
# Cubre los formatos reales vistos en el buzón (2026-08-11):
#   a) SIP_MANO_OBRA / balance_integrado (generado por nuevo/): metadatos
#      "Ticket / PDL:" / "OdM / Grafo:" / "LCL:" + tabla Nodo/Código/Cantidad,
#      y hoja LIQUIDACION con líderes (Cédula/Nombre/Porcentaje).
#   b) PRESUPUESTO del supervisor: hoja "Presupuesto", cabecera hacia la fila
#      24 (TIPO ACTIVIDAD | ... | SOLICITUD | NODO | ... | CODIGO E4E | ... |
#      CANT), el DF va en la columna SOLICITUD.

_HOJAS_MO_PRIORIDAD = [
    "SIP_MANO_OBRA", "MANO_OBRA", "PRESTACIONES", "ACTIVIDADES",
    "LIQ", "PRESUPUESTO", "LIQUIDACION",
]
_MAX_FILAS_CABECERA = 45
_MARCADORES_NODO = {"NODO", "PROYECTO", "PROJECT", "ODM"}
_MARCADORES_COD = {"CODIGO", "COD", "CODE", "PRESTACION", "ACTIVIDAD", "ACTIVITY"}
# "QUANTITY" explícito: ni CANT ni QTY son subcadenas de "quantity" (caso real
# 2026-08-27: la col "quantity" no calzaba y "TOTAL MO" —dinero— entró como
# cantidad: 1 fila con qty $4.951.997). "TOTAL" queda solo como último recurso.
_MARCADORES_QTY = {"CANTIDAD", "CANT", "QTY", "QUANTITY", "UNIDADES"}
_MARCADORES_QTY_DEBIL = {"TOTAL"}
_MARCADORES_LIDER = {"LIDER", "RESPONSABLE", "TECNICO", "PARTNER"}
_MARCADORES_SOLICITUD = {"SOLICITUD"}
_EXCL_BASE = {"DESC", "OBSERV", "NOTA"}
_EXCL_COD = _EXCL_BASE | {"TIPO", "NOMBRE"}     # "TIPO ACTIVIDAD" no es la col de código
_EXCL_QTY = _EXCL_BASE | {"VALOR", "NOMBRE"}    # "VALOR TOTAL" no es cantidad
_EXCL_NODO = {"TIPO"}                            # "TIPO NODO" (nodo_red/tramo_red) no es el nodo
_EXCL_LIDER = {"GRUPO"}                          # "GRUPO TECNICO" (LDD) no es el líder
_PRIO_LABELS_CODIGO = [("TICKET", "PDL"), ("ODM", "GRAFO"), ("LCL",)]


def _sin_tildes(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def _col_match(v: str, marcadores: set, excluir: set = None) -> bool:
    v_norm = _sin_tildes(v)
    excluir = excluir or set()
    if any(e in v_norm for e in excluir):
        return False
    return any(m in v_norm for m in marcadores)


def _metadatos_codigo(ws) -> dict:
    """Metadatos label:valor (col A/B) de las primeras filas. Devuelve
    {"codigo", "pdl"} POR SEPARADO — bug grave del 20-ago: devolver un solo
    valor hacía que "Ticket/PDL" (CDOPP...) enmascarara el "LCL/ODM" de la
    misma planilla, el árbitro caía a la cadena del correo (que mezcla PIs
    viejos del mismo hilo) y validaba el proyecto EQUIVOCADO.
    Nota: en planillas MI el label Ticket/PDL trae el DF — eso es código."""
    filas = []
    for fila in ws.iter_rows(min_row=1, max_row=25):
        filas.append([str(c.value if c.value is not None else "").upper().strip()
                      for c in fila[:3]])
    codigo, pdl = "", ""
    for grupo in _PRIO_LABELS_CODIGO:
        for vals in filas:
            label = _sin_tildes(vals[0]) if vals else ""
            if not any(lbl in label for lbl in grupo):
                continue
            val = vals[1].strip() if len(vals) > 1 else ""
            if not val or val == "0":
                continue
            if val.startswith("CDOPP"):
                pdl = pdl or val                     # PDL (o lista "CDOPP...;...")
                continue
            # Solo cuenta como código lo que calce los patrones reales — el
            # label OdM/Grafo trae números de grafo ("83918418;83918420") que
            # NO son proyecto.
            m = RE_DF.search(val)
            if m:
                codigo = codigo or f"DF{m.group(1)}"
                continue
            m = RE_LCL.search(val)
            if m:
                codigo = codigo or f"LCL{m.group(1)}"
                continue
            if val.isdigit() and len(val) >= 8 and "LCL" in grupo:
                codigo = codigo or ("LCL" + val)     # número pelado bajo label LCL
    return {"codigo": codigo, "pdl": pdl}


def _lideres_hoja_liquidacion(wb) -> list:
    """Bloque 'LIDERES A LIQUIDAR' (Cédula/Nombre/Porcentaje) de la hoja
    LIQUIDACION del balance_integrado. Devuelve [{cedula, nombre, pct}]."""
    nombre_hoja = next((s for s in wb.sheetnames if s.upper() == "LIQUIDACION"), None)
    if not nombre_hoja:
        return []
    ws = wb[nombre_hoja]
    out, en_tabla = [], False
    col_ced = col_nom = col_pct = None
    for fila in ws.iter_rows(min_row=1, max_row=40):
        vals = [str(c.value if c.value is not None else "").strip() for c in fila[:6]]
        arriba = [_sin_tildes(v.upper()) for v in vals]
        if not en_tabla:
            if any("CEDULA" in v for v in arriba) and any("NOMBRE" in v for v in arriba):
                col_ced = next(i for i, v in enumerate(arriba) if "CEDULA" in v)
                col_nom = next(i for i, v in enumerate(arriba) if "NOMBRE" in v)
                col_pct = next((i for i, v in enumerate(arriba) if "PORCENTAJE" in v or "%" in v), None)
                en_tabla = True
            continue
        nom = vals[col_nom] if col_nom < len(vals) else ""
        if not nom or nom.upper() == "TOTAL":
            if out:
                break
            continue
        ced = vals[col_ced] if col_ced < len(vals) else ""
        pct = 0.0
        if col_pct is not None and col_pct < len(vals):
            try:
                pct = float(str(vals[col_pct]).replace("%", "").replace(",", ".").strip() or 0)
            except ValueError:
                pct = 0.0
        out.append({"cedula": ced.lstrip("Cc") if ced[:1] in "Cc" and ced[1:].isdigit() else ced,
                    "nombre": nom, "pct": pct})
    return out


def _fecha_hoja_liquidacion(wb) -> str:
    """'Fecha real de ejec...' de la hoja LIQUIDACION (balance_integrado)."""
    nombre = next((s for s in wb.sheetnames if s.upper() == "LIQUIDACION"), None)
    if not nombre:
        return ""
    ws = wb[nombre]
    for fila in ws.iter_rows(min_row=1, max_row=25):
        lbl = _sin_tildes(str(fila[0].value or "").upper())
        if "FECHA" in lbl and ("EJEC" in lbl or "REAL" in lbl):
            v = fila[1].value if len(fila) > 1 else None
            if v is None:
                return ""
            if hasattr(v, "strftime"):
                return v.strftime("%Y-%m-%d")
            s = str(v).strip()
            m = re.search(r"\d{4}-\d{2}-\d{2}", s)
            if m:
                return m.group(0)
            m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
            if m:
                return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
            return s[:20]
    return ""


def _filas_ocultas_por_hoja(ruta_xlsx: str) -> dict:
    """{nombre_hoja: set de índices (1-based) de filas OCULTAS}. Los supervisores
    mandan el PRESUPUESTO completo (~1300 filas) con un filtro de Excel que deja
    visibles solo las filas a liquidar — lo visible ES lo que se cobra. openpyxl
    en read_only no expone hidden, así que se lee el XML crudo del .xlsx."""
    ocultas = {}
    ns_main = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    ns_rid = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    try:
        with zipfile.ZipFile(ruta_xlsx) as z:
            rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
            rid2target = {rel.get("Id"): rel.get("Target") or "" for rel in rels}
            wb_xml = ElementTree.fromstring(z.read("xl/workbook.xml"))
            for sh in wb_xml.iter(f"{ns_main}sheet"):
                target = rid2target.get(sh.get(ns_rid), "")
                if not target:
                    continue
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target
                filas = set()
                try:
                    with z.open(target) as fh:
                        for _, el in ElementTree.iterparse(fh):
                            if el.tag == f"{ns_main}row":
                                if el.get("hidden") in ("1", "true") and el.get("r"):
                                    filas.add(int(el.get("r")))
                                el.clear()
                except (KeyError, ElementTree.ParseError, ValueError):
                    continue
                if filas:
                    ocultas[sh.get("name")] = filas
    except Exception:
        return {}
    return ocultas


def _txt_celda(v) -> str:
    """Valor de celda como texto SIN perder el cero: el nodo 0 llega como
    número 0 y `str(v or "")` lo volvía vacío (caso real pérdidas mineras
    2-oct: 4 prestaciones del nodo 0 por $7,4M se quedaron sin nodo)."""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


_RE_NODO_CABLE = re.compile(
    r"^(?:CABLE|TRAMO|VANO|RED)\s*N?\s*(\d+[A-Z]?)\s*(?:-|_|AL|A)\s*N?\s*(\d+[A-Z]?)$",
    re.IGNORECASE)


def _nodo_excel(valor: str) -> str:
    """Etiqueta de nodo tal como la entienden nuevo/ y liquidador2:
    'Cable 0-1' / 'TRAMO 19_24' / 'cable N54 al N56' → 'N0-N1' / 'N19-N24' /
    'N54-N56' (en SGS son los proyectos N0_N1…). Lo demás queda igual."""
    s = str(valor or "").strip()
    m = _RE_NODO_CABLE.match(s)
    if m:
        return f"N{m.group(1).upper()}-N{m.group(2).upper()}"
    return s


def _parsear_hoja_mo(ws, filas_ocultas=None, marcador_col=None) -> dict:
    """Busca la tabla de MO (cabecera con Código+Cantidad, filas QP) en UNA
    hoja. Las hojas RG11 reales traen DOS tablas lado a lado (presupuesto
    crudo COD_PREST/QUANTITY a la izquierda; tabla de trabajo CODIGO NUEVO/
    CANT del supervisor a la derecha, bajo el rótulo 'TOTAL MO PDL...') — se
    arman PARES (código+cantidad+nodo por cercanía de columnas) y gana el par
    más cercano al rótulo del PDL (marcador_col) o, sin rótulo, el que dé más
    prestaciones QP."""
    max_row = ws.max_row or 0
    header_row = None
    nodos_c, cods_c, qtys_c, qdeb_c, lids_c, sols_c, pdls_c = [], [], [], [], [], [], []

    for r, fila in enumerate(ws.iter_rows(min_row=1, max_row=min(max_row, _MAX_FILAS_CABECERA)), 1):
        vals = [str((c.value if c.value is not None else "")).upper().strip() for c in fila[:30]]
        nodos_c = [i for i, v in enumerate(vals) if _col_match(v, _MARCADORES_NODO, _EXCL_NODO)]
        cods_c = [i for i, v in enumerate(vals) if _col_match(v, _MARCADORES_COD, _EXCL_COD)]
        qtys_c = [i for i, v in enumerate(vals) if _col_match(v, _MARCADORES_QTY, _EXCL_QTY)]
        qdeb_c = [i for i, v in enumerate(vals) if _col_match(v, _MARCADORES_QTY_DEBIL, _EXCL_QTY)]
        lids_c = [i for i, v in enumerate(vals) if _col_match(v, _MARCADORES_LIDER, _EXCL_LIDER)]
        sols_c = [i for i, v in enumerate(vals) if _col_match(v, _MARCADORES_SOLICITUD)]
        pdls_c = [i for i, v in enumerate(vals) if v == "PDL" or v.startswith("PDL ")]
        if cods_c and (qtys_c or qdeb_c):
            header_row = r
            break

    if header_row is None:
        return {"filas": [], "filas_mat": [], "lideres": [],
                "descartadas": 0, "solicitud": "",
                "ocultas": 0, "sospechosas": 0, "sin_cabecera": True}

    filas_ocultas = filas_ocultas or set()

    def _cerca(pool, ref, lado=""):
        if not pool:
            return None
        if lado == "der":
            der = [p for p in pool if p >= ref]
            if der:
                return min(der)
        if lado == "izq":
            izq = [p for p in pool if p <= ref]
            if izq:
                return max(izq)
        return min(pool, key=lambda p: abs(p - ref))

    qty_pool = qtys_c or qdeb_c
    pares = []
    for ic in cods_c:
        pares.append({"cod": ic,
                      "qty": _cerca(qty_pool, ic, "der"),
                      "nodo": _cerca(nodos_c, ic, "izq"),
                      "lider": _cerca(lids_c, ic),
                      "sol": _cerca(sols_c, ic),
                      "pdl": _cerca(pdls_c, ic)})
    pares = [p for p in pares if p["qty"] is not None]
    if not pares:
        return {"filas": [], "filas_mat": [], "lideres": [],
                "descartadas": 0, "solicitud": "",
                "ocultas": 0, "sospechosas": 0, "sin_cabecera": True}

    def _color_celda(c) -> str:
        """'RRGGBB' del relleno sólido de la celda, 'theme' si es color de
        tema (no resoluble) o '' sin relleno. Los supervisores marcan con
        color qué filas liquidar ("los nodos que están en verde")."""
        try:
            f = c.fill
            if not f or not f.fill_type or f.fill_type == "none":
                return ""
            fg = f.fgColor
            if fg is not None and fg.type == "rgb" and fg.rgb:
                rgb = str(fg.rgb).upper()
                return rgb[-6:] if len(rgb) >= 6 else ""
            return "theme"
        except Exception:
            return ""

    def _escanear(col):
        """Recorre la tabla usando el par de columnas `col`."""
        idx_cod = col["cod"]
        filas, filas_mat, lideres, descartadas = [], [], [], 0
        colores, colores_mat = [], []   # relleno de la celda de código, alineado
        solicitud, ocultas_qp, sospechosas = "", 0, 0
        pdl_filas = {}   # PDL por fila (col "PDL") → nodos que cubre
        # TODAS las prestaciones QP en orden de Excel, ignorando el filtro.
        # Sirve para detectar un filtro OLVIDADO (caso real tutela Susa 29-sep:
        # el RG11 quedó filtrado en nodos 2 y 24 pero el correo declara del 1
        # al 24) — la decisión la toma el analizador contra el correo.
        filas_todas = []

        def _qty_valida(fila):
            q_raw = fila[col["qty"]].value if col["qty"] is not None and col["qty"] < len(fila) else None
            if q_raw is None:
                return None
            try:
                q = float(str(q_raw).replace(",", "."))
            except (TypeError, ValueError):
                return None
            return q if 0 < q < 100_000 else None

        for r, fila in enumerate(ws.iter_rows(min_row=header_row + 1, max_row=max_row),
                                 header_row + 1):
            def _celda(idx):
                return _txt_celda(fila[idx].value) if idx is not None and idx < len(fila) else ""
            cod = _celda(idx_cod)
            if not cod:
                continue
            # Fila oculta por filtro de Excel = el supervisor la excluyó de
            # esta liquidación. No se cobra (salvo que el analizador demuestre
            # que el filtro contradice el alcance declarado en el correo).
            if r in filas_ocultas:
                if re.match(r"^QP\d+", cod, re.IGNORECASE):
                    ocultas_qp += 1
                    q_oc = _qty_valida(fila)
                    if q_oc is not None:
                        filas_todas.append((_nodo_excel(_celda(col["nodo"])), cod.upper(), q_oc))
                continue
            lid = _celda(col["lider"])
            if lid:
                # "NOMBRE1 APELLIDO1 + NOMBRE2 APELLIDO2" = DOS personas
                # (caso real 1-sep) — partir por +, /, ; o " y "
                for nom in re.split(r"\s*\+\s*|\s*/\s*|\s*;\s*|\s+[Yy]\s+", lid):
                    nom = nom.strip()
                    if nom and nom.upper() not in {x.upper() for x in lideres}:
                        lideres.append(nom)
            # Filtro duro: solo prestaciones QP — evita tablas genéricas.
            if not re.match(r"^QP\d+", cod, re.IGNORECASE):
                # Material FÍSICO del supervisor (código SAP de 6 dígitos):
                # SOLO para el pre-análisis MAT — jamás cantidad autorizada.
                if re.fullmatch(r"\d{6}", cod):
                    q_raw = fila[col["qty"]].value if col["qty"] is not None and col["qty"] < len(fila) else None
                    try:
                        q = float(str(q_raw).replace(",", "."))
                    except (TypeError, ValueError):
                        q = 0.0
                    if 0 < q < 100_000:
                        filas_mat.append((_nodo_excel(_celda(col["nodo"])), cod, q))
                        colores_mat.append(_color_celda(fila[idx_cod]))
                descartadas += 1
                continue
            if not solicitud:
                solicitud = _celda(col["sol"])
            qty_raw = fila[col["qty"]].value if col["qty"] is not None and col["qty"] < len(fila) else None
            if qty_raw is None:
                continue
            try:
                qty = float(str(qty_raw).replace(",", "."))
            except (TypeError, ValueError):
                continue
            if qty == 0:
                continue
            # Guardián: "cantidad" de 100.000+ es dinero, no cantidad.
            if qty >= 100_000:
                sospechosas += 1
                continue
            nodo_val = _nodo_excel(_celda(col["nodo"]))
            pdl_fila = _celda(col["pdl"]).upper()
            if pdl_fila.startswith("CDOPP"):
                pdl_filas.setdefault(pdl_fila, [])
                if nodo_val and nodo_val not in pdl_filas[pdl_fila]:
                    pdl_filas[pdl_fila].append(nodo_val)
            filas.append((nodo_val, cod.upper(), qty))
            filas_todas.append((nodo_val, cod.upper(), qty))
            colores.append(_color_celda(fila[idx_cod]))
        return {"filas": filas, "filas_mat": filas_mat, "lideres": lideres,
                "colores": colores, "colores_mat": colores_mat,
                "filas_todas": filas_todas,
                "descartadas": descartadas, "solicitud": solicitud,
                "pdl_filas": pdl_filas,
                "ocultas": ocultas_qp, "sospechosas": sospechosas,
                "sin_cabecera": False}

    # Elegir el par ganador: con rótulo "TOTAL MO PDL" el par más cercano al
    # rótulo (esa es la tabla de trabajo del supervisor — caso real tutela
    # 1-sep: presupuesto 98/98/98 vs tabla PDL 180/60/38); sin rótulo, el par
    # con más prestaciones QP.
    resultados = [(_escanear(p), p) for p in pares]
    con_filas = [(out, p) for out, p in resultados if out["filas"]]
    if not con_filas:
        return resultados[0][0]
    if marcador_col is not None and len(con_filas) > 1:
        return min(con_filas, key=lambda x: abs(x[1]["cod"] - marcador_col))[0]
    return max(con_filas, key=lambda x: len(x[0]["filas"]))[0]


_LIBROS_LIMPIOS = {}   # ruta original → copia sin definedNames


def _copia_sin_defined_names(ruta_xlsx: str) -> str:
    """Copia del .xlsx con el bloque <definedNames> eliminado de
    xl/workbook.xml. Los nombres definidos no hacen falta para leer datos."""
    if ruta_xlsx in _LIBROS_LIMPIOS and os.path.exists(_LIBROS_LIMPIOS[ruta_xlsx]):
        return _LIBROS_LIMPIOS[ruta_xlsx]
    destino = os.path.join(tempfile.gettempdir(), f"centro_mo_limpio_{uuid.uuid4().hex}.xlsx")
    with zipfile.ZipFile(ruta_xlsx) as zin, zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            datos = zin.read(info.filename)
            if info.filename == "xl/workbook.xml":
                datos = re.sub(rb"<definedNames>.*?</definedNames>", b"", datos, flags=re.S)
            zout.writestr(info, datos)
    _LIBROS_LIMPIOS[ruta_xlsx] = destino
    return destino


def _abrir_libro(ruta_xlsx: str, **kw):
    """openpyxl.load_workbook con red de seguridad. Caso real de un RG11
    (2026-09-02): 20.602 definedNames basura ('\\0' → #REF!) y un Print_Titles
    '#N/A' hacían abortar la carga ("could not assign names"). Se reintenta
    con una copia sin <definedNames>."""
    try:
        return openpyxl.load_workbook(ruta_xlsx, **kw)
    except Exception as e:
        msg = f"{e} {getattr(e, '__cause__', '') or ''}".lower()
        if not any(s in msg for s in ("assign names", "definedname", "print titles",
                                       "invalid xml", "print area")):
            raise
    return openpyxl.load_workbook(_copia_sin_defined_names(ruta_xlsx), **kw)


RE_PDL_HOJA = re.compile(r"CDOPP[A-Z0-9]{6,14}", re.IGNORECASE)


def _pdl_marcador_hoja(ws):
    """PDL declarado en la cabecera de la hoja (celda tipo 'TOTAL MO PDL
    CDOPP2026R08240') — señal de que esa hoja/tabla es la de TRABAJO del
    supervisor para esta liquidación, no el presupuesto.
    Devuelve (CDOPP, índice de columna 0-based) o ("", None)."""
    try:
        for fila in ws.iter_rows(min_row=1, max_row=8):
            for j, c in enumerate(fila[:30]):
                v = str(c.value or "")
                if "PDL" in v.upper():
                    m = RE_PDL_HOJA.search(v)
                    if m:
                        return m.group(0).upper(), j
    except Exception:
        pass
    return "", None


def extraer_de_excel(ruta_xlsx: str) -> dict:
    """Extrae de una planilla: {codigo, filas [(nodo, qp, cant)], lideres
    (nombres), lideres_det [{cedula,nombre,pct}], errores}. Recorre TODAS las
    hojas (prioritarias primero) hasta encontrar la tabla de MO."""
    wb = _abrir_libro(ruta_xlsx, data_only=True, read_only=True)
    ocultas_hoja = _filas_ocultas_por_hoja(ruta_xlsx)

    # Solo hojas de datos: una hoja de GRÁFICO (Chartsheet) no tiene iter_rows
    # y reventaba todo el análisis (caso real 2-sep, "Gráfico1").
    hojas = [ws.title for ws in wb.worksheets]
    orden, vistos = [], set()
    for nombre in _HOJAS_MO_PRIORIDAD:
        for s in hojas:
            if s.upper() == nombre and s not in vistos:
                orden.append(s)
                vistos.add(s)
    orden += [s for s in hojas if s not in vistos]

    filas, lideres, errores, avisos = [], [], [], []
    filas_mat = []
    codigo, pdl, solicitud, descartadas_max = "", "", "", 0

    # Se evalúan TODAS las hojas con tabla de prestaciones y entre ellas gana
    # la que declara el subtotal del PDL ("TOTAL MO PDL CDOPPxxxx") — es la
    # hoja de TRABAJO que el supervisor filtró para ESTA liquidación. Caso real
    # tutela 6301024442 (1-sep): RG11_budget (presupuesto, 27 filas, cantidades
    # viejas) aparecía antes que MDO&MAT (13 filas del PDL, cantidades reales)
    # y el corte en la primera hoja liquidaba de más.
    candidatas = []
    for nombre in orden:
        ws = wb[nombre]
        if not codigo or not pdl:
            meta = _metadatos_codigo(ws)
            codigo = codigo or meta["codigo"]
            pdl = pdl or meta["pdl"]
        marcador, mcol = _pdl_marcador_hoja(ws)
        res = _parsear_hoja_mo(ws, ocultas_hoja.get(nombre), mcol)
        descartadas_max = max(descartadas_max, res["descartadas"])
        if res["filas"]:
            candidatas.append((nombre, res, marcador))

    elegida = None
    colores, colores_mat, filas_todas = [], [], []
    if candidatas:
        con_pdl = [c for c in candidatas if c[2]]
        elegida = con_pdl[0] if con_pdl else candidatas[0]
        nombre, res, marcador = elegida
        filas = res["filas"]
        lideres = res["lideres"]
        solicitud = res["solicitud"]
        colores = list(res.get("colores") or [])
        filas_todas = list(res.get("filas_todas") or [])
        filas_mat.extend(res.get("filas_mat") or [])
        colores_mat.extend(res.get("colores_mat") or [])
        if marcador and not pdl:
            pdl = marcador
        # PDL por fila (columna "PDL" de la tabla — patrón Juan Carlos 1-sep):
        # uno solo → es EL PDL de la planilla; varios → cada tramo es una
        # liquidación distinta y hay que dividir.
        pdl_filas = res.get("pdl_filas") or {}
        if len(pdl_filas) == 1:
            pdl = pdl or next(iter(pdl_filas))
        elif len(pdl_filas) > 1:
            mapa = " · ".join(f"{k} → nodos {', '.join(v)}" for k, v in pdl_filas.items())
            avisos.append(
                f"⚠ La planilla trae {len(pdl_filas)} PDL DISTINTOS: {mapa} — "
                "son liquidaciones separadas: usa ✎ (solo nodos + PDL + fecha) "
                "para generar una planilla por PDL")
        if len(candidatas) > 1 and elegida is not candidatas[0]:
            avisos.append(
                f"Se usó la hoja '{nombre}' (declara el subtotal del PDL {marcador}) "
                f"en vez de '{candidatas[0][0]}' — el presupuesto trae otras "
                "filas/cantidades")
        if res["ocultas"]:
            avisos.append(
                f"Filtro de Excel en '{nombre}': {res['ocultas']} prestaciones QP "
                f"ocultas ignoradas — se liquidan solo las {len(filas)} visibles")
        if res.get("sospechosas"):
            avisos.append(
                f"⚠ Hoja '{nombre}': {res['sospechosas']} filas con cantidad "
                f"≥100.000 descartadas (parecen VALORES en pesos, no cantidades) "
                "— revisar la planilla a mano")

    # Materiales físicos: hoja SIP_MATERIALES aparte (balance_integrado)
    for s in hojas:
        if s.upper() == "SIP_MATERIALES" and (not elegida or s != elegida[0]):
            try:
                res_m = _parsear_hoja_mo(wb[s], ocultas_hoja.get(s))
                filas_mat.extend(res_m.get("filas_mat") or [])
                colores_mat.extend(res_m.get("colores_mat") or [])
            except Exception:
                pass

    lideres_det = _lideres_hoja_liquidacion(wb)
    for ld in lideres_det:
        if ld["nombre"].upper() not in {x.upper() for x in lideres}:
            lideres.append(ld["nombre"])

    # El PRESUPUESTO trae el DF en la columna SOLICITUD de la tabla de MO
    if not codigo and solicitud:
        m = RE_DF.search(solicitud) or RE_LCL.search(solicitud)
        if m:
            codigo = m.group(0).upper().replace(" ", "").replace("_", "").replace("-", "")

    if not filas:
        if descartadas_max:
            errores.append(
                f"Se detectaron tablas pero ninguna fila con código QP ({descartadas_max} "
                "descartadas) — no parece haber planilla de prestaciones en este archivo")
        else:
            errores.append("No se encontró tabla de MO (Código+Cantidad) en ninguna hoja")

    return {"codigo": codigo, "pdl": pdl, "filas": filas, "filas_mat": filas_mat,
            "colores": colores, "colores_mat": colores_mat,
            "filas_todas": filas_todas,
            "errores": errores, "avisos": avisos, "lideres": lideres,
            "lideres_det": lideres_det,
            "fecha_ejecucion": _fecha_hoja_liquidacion(wb)}


# ---------------------------- planilla NATIVA (generada por el módulo Balance)
# Caso de los supervisores que generan la planilla con el Balance: el adjunto
# YA ES la planilla final — no se pasa por las heurísticas genéricas (salían
# mal) y no hay nada que regenerar. Se detecta por ESTRUCTURA (hoja MO_MANUAL),
# no por remitente ni por nombre, porque no siempre la manda.

def tipo_planilla_valida(ruta_xlsx: str) -> str:
    """Planillas nacidas del módulo Balance:
    'MO_MANUAL' = balance_sgs_manual_* · 'INTEGRADO' = balance_integrado_*
    (hojas LIQUIDACION + SIP_MANO_OBRA) · '' = no es del sistema.
    Ambas se usan TAL CUAL (el archivo original es la planilla) — liquidador2
    las entiende nativamente y es quien mejor detecta parciales/adiciones."""
    try:
        wb = _abrir_libro(ruta_xlsx, read_only=True)
        nombres = {s.upper() for s in wb.sheetnames}
        wb.close()
        if "MO_MANUAL" in nombres:
            return "MO_MANUAL"
        if "SIP_MANO_OBRA" in nombres and "LIQUIDACION" in nombres:
            return "INTEGRADO"
        return ""
    except Exception:
        return ""


def es_planilla_nativa(ruta_xlsx: str) -> bool:
    return tipo_planilla_valida(ruta_xlsx) == "MO_MANUAL"


def extraer_planilla_nativa(ruta_xlsx: str) -> dict:
    """Lee la planilla balance_sgs_manual_* con su estructura EXACTA:
    metadatos (OdM/LCL, PDL, Supervisor, Fecha ejecución si existe), tabla de
    MO (Nodo / Código QP / Cantidad) y bloque de líderes (Cédula/Nombre/%)."""
    wb = _abrir_libro(ruta_xlsx, data_only=True)
    ws = wb["MO_MANUAL"]
    codigo = pdl = fecha = supervisor = ""
    filas, lideres_det, errores = [], [], []
    col = {}
    header = None
    lid_cols = None

    for f in ws.iter_rows(min_row=1, max_row=ws.max_row):
        a = str(f[0].value if f and f[0].value is not None else "").strip()
        b = f[1].value if len(f) > 1 else None
        an = _sin_tildes(a.upper())

        if an.startswith("ODM"):
            v = str(b or "").strip()
            codigo = ("LCL" + v) if (v.isdigit() and len(v) >= 8) else v
        elif an.startswith("PDL"):
            v = str(b or "").strip()
            pdl = "" if v in ("", "-", "—") else v
        elif an.startswith("SUPERVISOR"):
            supervisor = str(b or "").strip()
        elif an.startswith("FECHA EJEC"):
            fecha = b.strftime("%Y-%m-%d") if hasattr(b, "strftime") else str(b or "").strip()[:10]

        vals = [_sin_tildes(str(c.value or "").upper().strip()) for c in f[:10]]

        # Cabecera del bloque de líderes (Cédula / Nombre / %)
        if lid_cols is None and any("CEDULA" in v for v in vals) and any("NOMBRE" in v for v in vals):
            lid_cols = {
                "ced": next(i for i, v in enumerate(vals) if "CEDULA" in v),
                "nom": next(i for i, v in enumerate(vals) if "NOMBRE" in v),
                "pct": next((i for i, v in enumerate(vals) if "%" in v or "LIQUIDACION" in v or "PORCENTAJE" in v), None),
            }
            continue
        if lid_cols is not None:
            nom = str(f[lid_cols["nom"]].value or "").strip() if lid_cols["nom"] < len(f) else ""
            if nom and nom.upper() != "TOTAL":
                ced = str(f[lid_cols["ced"]].value or "").strip() if lid_cols["ced"] < len(f) else ""
                if ced[:1] in "Cc" and ced[1:].isdigit():
                    ced = ced[1:]
                pct = 0.0
                if lid_cols["pct"] is not None and lid_cols["pct"] < len(f):
                    try:
                        pct = float(str(f[lid_cols["pct"]].value or "0").replace("%", "").replace(",", ".").strip() or 0)
                    except ValueError:
                        pct = 0.0
                lideres_det.append({"cedula": ced if ced.isdigit() else "",
                                    "nombre": nom, "pct": pct})
            continue

        # Cabecera de la tabla de MO
        if header is None and vals and vals[0] == "#":
            for i, v in enumerate(vals):
                if v == "NODO":
                    col["nodo"] = i
                elif "CODIGO" in v:
                    col["qp"] = i
                elif "CANTIDAD" in v:
                    col["qty"] = i
            if "qp" in col and "qty" in col:
                header = True
            continue
        if header and "qp" in col:
            qp = str(f[col["qp"]].value or "").strip() if col["qp"] < len(f) else ""
            if re.match(r"^QP\d+", qp, re.IGNORECASE):
                nodo = str(f[col.get("nodo", 1)].value or "").strip() if col.get("nodo", 1) < len(f) else ""
                try:
                    qty = float(f[col["qty"]].value)
                except (TypeError, ValueError, IndexError):
                    continue
                if qty:
                    filas.append((nodo, qp.upper(), qty))

    if not filas:
        errores.append("Planilla nativa sin filas de MO — revisar el archivo")
    return {"codigo": codigo, "pdl": pdl, "filas": filas,
            "lideres": [d["nombre"] for d in lideres_det],
            "lideres_det": lideres_det, "fecha_ejecucion": fecha,
            "supervisor": supervisor, "errores": errores}


# ---------------------------------------------------------------- escaneo Outlook

# Progreso del escaneo en curso, leído por /orden-pendiente/progreso mientras
# el POST de búsqueda sigue bloqueado (el recorrido completo tarda 1-3 min).
PROGRESO = {"activo": False, "carpeta": "", "carpetas": 0,
            "revisados": 0, "candidatos": 0, "inicio": 0.0}


def escanear_correos_orden_pendiente() -> list:
    """Correos del mes actual con bandera roja o categoría 'Orden Pendiente',
    clasificados MI/Programados/Desconocido, con lo que se pudo extraer del
    cuerpo/adjunto. No modifica nada en Outlook."""
    pythoncom.CoInitialize()
    resultados = []
    PROGRESO.update(activo=True, carpeta="", carpetas=0,
                    revisados=0, candidatos=0, inicio=time.time())
    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        ns = outlook.GetNamespace("MAPI")
        hoy = datetime.now()
        primer_dia = datetime(hoy.year, hoy.month, 1)

        for store_root in ns.Folders:
            nombre_store = (store_root.Name or "").lower()
            if "públicas" in nombre_store or "public" in nombre_store:
                continue
            for ruta, carpeta in _carpetas_de_correo(store_root):
                PROGRESO["carpeta"] = ruta
                PROGRESO["carpetas"] += 1
                try:
                    items = carpeta.Items
                    items.Sort("[ReceivedTime]", True)
                    item = items.GetFirst()
                except Exception:
                    continue
                while item is not None:
                    try:
                        if item.Class != OL_MAIL:
                            item = items.GetNext()
                            continue
                        try:
                            fecha_dt = item.ReceivedTime.replace(tzinfo=None)
                        except Exception:
                            item = items.GetNext()
                            continue
                        if fecha_dt < primer_dia:
                            break  # orden descendente: el resto es más viejo

                        PROGRESO["revisados"] += 1
                        marcado = getattr(item, "FlagStatus", 0) == 2
                        categorias = (getattr(item, "Categories", "") or "").lower()
                        etiquetado = "orden pendiente" in categorias
                        if marcado or etiquetado:
                            candidato = _procesar_candidato(item, carpeta, ruta, marcado, etiquetado)
                            resultados.append(candidato)
                            PROGRESO["candidatos"] = len(resultados)
                    except Exception:
                        pass
                    item = items.GetNext()
    finally:
        PROGRESO["activo"] = False
        pythoncom.CoUninitialize()

    resultados.sort(key=lambda r: r["fecha"], reverse=True)
    return resultados


def _procesar_candidato(item, carpeta, ruta, marcado, etiquetado):
    asunto = item.Subject or ""
    try:
        cuerpo = item.Body or ""
    except Exception:
        cuerpo = ""
    try:
        fecha = item.ReceivedTime.strftime("%Y-%m-%d %H:%M")
    except Exception:
        fecha = ""

    codigo, tipo, fuente = _detectar_codigo(asunto, "asunto")
    if not codigo:
        codigo, tipo, fuente = _detectar_codigo(cuerpo, "cuerpo")

    pdl = ""
    filas_mo = []
    errores_excel = []
    adjunto_usado = ""

    try:
        adjuntos = list(item.Attachments)
    except Exception:
        adjuntos = []

    for adj in adjuntos:
        nombre = adj.FileName or ""
        if not nombre.lower().endswith((".xlsx", ".xlsm")):
            continue
        ruta_tmp = os.path.join(tempfile.gettempdir(), f"cazador_mo_{uuid.uuid4().hex}.xlsx")
        datos = None
        try:
            adj.SaveAsFile(ruta_tmp)
            datos = extraer_de_excel(ruta_tmp)
        except Exception as e:
            errores_excel.append(f"{nombre}: {e}")
        finally:
            try:
                os.remove(ruta_tmp)
            except OSError:
                pass

        if not datos:
            continue
        errores_excel.extend(datos["errores"])
        if datos["codigo"]:
            tipo_excel = _clasificar(datos["codigo"])
            if not codigo and tipo_excel:
                codigo, tipo, fuente = datos["codigo"], tipo_excel, "adjunto"
            elif datos["codigo"].upper() != codigo.upper():
                pdl = datos["codigo"]
        if datos["filas"] and not filas_mo:
            filas_mo = datos["filas"]
            adjunto_usado = nombre

    return {
        "entry_id": item.EntryID,
        "store_id": carpeta.StoreID,
        "asunto": asunto,
        "remitente": item.SenderName or "",
        "fecha": fecha,
        "carpeta": ruta,
        "codigo": codigo,
        "tipo": tipo or "Desconocido",
        "fuente_codigo": fuente,
        "pdl": pdl,
        "adjunto": adjunto_usado,
        "filas_mo": [{"nodo": n, "codigo": c, "cantidad": q} for n, c, q in filas_mo],
        "errores_excel": errores_excel,
        "bandera": marcado,
        "categoria": etiquetado,
    }


# --------------------------------------------- validación de códigos contra SGS

_sgs_cliente_cache = None


def _sgs_cliente():
    """Cliente SGS cacheado (solo lectura). Reautentica si la sesión murió."""
    global _sgs_cliente_cache
    from integracion import cfg_desde_archivo
    from sgs_client import SGSClient
    if _sgs_cliente_cache is not None:
        try:
            _sgs_cliente_cache._call_kw("res.users", "read",
                                        [[_sgs_cliente_cache.uid]], {"fields": ["id"]})
            return _sgs_cliente_cache
        except Exception:
            _sgs_cliente_cache = None
    cfg = cfg_desde_archivo()
    c = SGSClient(cfg["sgs_url"], cfg["sgs_db"], cfg["sgs_login"], cfg["sgs_password"])
    c.autenticar()
    _sgs_cliente_cache = c
    return c


def _buscar_proyecto_flexible(cliente, codigo: str):
    """Busca un proyecto padre probando, en orden: order_number/code exacto,
    DF sin el último dígito (SGS a veces lo guarda truncado), y el número
    dentro del NOMBRE del proyecto (patrón NNDD: name='6301018664//WR-24532'
    con order_number=WR-24532). Devuelve (proyecto|None, metodo)."""
    cod = (codigo or "").upper()
    limpio = cod[3:].lstrip("_- ") if cod.startswith("LCL") else cod
    proy = cliente.buscar_por_codigo(limpio)
    if proy:
        return proy, "order_number"
    if cod.startswith("DF") and len(limpio) >= 12:
        try:
            proy = cliente.buscar_por_codigo(limpio[:-1])
        except Exception:
            proy = None
        if proy:
            return proy, "DF truncado"
    # Respaldo por NOMBRE — cubre dos casos reales: (a) NNDD con LCL en el
    # name y order_number=WR-xxxxx; (b) proyectos creados con order_number='0'
    # y el DF completo en el name (caso DF202664645116, 31-ago). Solo acepta
    # hit ÚNICO para no adivinar.
    if len(limpio) >= 8:
        variantes = [limpio]
        digitos = re.sub(r"^\D+", "", limpio)
        if digitos != limpio and len(digitos) >= 8:
            variantes.append(digitos)
        if cod.startswith("DF") and len(limpio) >= 12:
            variantes.append(limpio[:-1])
        for v in dict.fromkeys(variantes):
            try:
                hits = cliente.search_read(
                    "project.dominion",
                    [["name", "ilike", v], ["parent_id", "=", False]],
                    ["id", "name", "code", "order_number"], limit=2)
            except Exception:
                hits = []
            if len(hits) == 1:
                return hits[0], "nombre"
    return None, ""


def _expandir_nodos_planilla(nodos) -> set:
    """Nodos de la planilla como conjunto comparable con los hijos SGS:
    '66al75' → {'66'..'75'} · 'N11-N17' → {'11'..'17'} · '25A' → {'25A'}."""
    out = set()
    for n in (nodos or []):
        s = _norm_nodo(n)
        if not s:
            continue
        m = re.match(r"^(\d+)(?:AL|-)(\d+)$", s)
        if m and int(m.group(1)) < int(m.group(2)) <= int(m.group(1)) + 200:
            out.update(str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1))
        else:
            out.add(s)
    return out


def _tokens_nombre_b(s: str) -> set:
    plano = _sin_tildes(str(s or "").upper())
    return {t for t in re.split(r"[^A-Z0-9]+", plano) if len(t) >= 3}


def validar_codigo_sgs(candidatos: list, nodos_planilla=None, remitente: str = ""):
    """La trazabilidad de los correos es sucia: una cadena puede traer varios
    DF/LCL y el "primero que aparece" suele ser el equivocado. Aquí SGS es el
    árbitro en dos pasos:

    1. Existencia: solo cuentan los candidatos que existen como proyecto,
       en orden de confianza de su fuente (planilla del sistema > asunto >
       cuerpo reciente > imagen > cadena vieja > encabezado de Excel RG11).
    2. Coherencia de nodos (regla 2026-08-24): los supervisores NO
       actualizan los encabezados de sus RG11 y los hilos se reutilizan entre
       PIs, así que pueden validar DOS proyectos reales. El correcto es el que
       CONTIENE los nodos que la planilla va a liquidar.

    candidatos: [{"codigo": "DF..."|"LCL...", "fuente": "asunto"|...}]
    nodos_planilla: nodos de las filas de MO (p. ej. ["74", "66al75"]).
    Devuelve (ganador|None, log, sgs_disponible):
      ganador = {"codigo", "fuente", "codigo_sgs", "proyecto"[, "coherencia_nodos"]}
    """
    log = []
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, [f"SGS no disponible para validar códigos: {str(e)[:80]}"], False

    existentes = []
    for c in candidatos:
        cod = c["codigo"].upper()
        limpio = cod[3:].lstrip("_- ") if cod.startswith("LCL") else cod
        try:
            proy, metodo = _buscar_proyecto_flexible(cliente, cod)
        except Exception as e:
            log.append(f"{cod}: error consultando SGS ({str(e)[:60]})")
            continue
        if proy:
            det = "" if metodo == "order_number" else f" (por {metodo})"
            log.append(f"✓ {cod} [{c['fuente']}] existe en SGS{det}: {proy.get('name', '')}")
            existentes.append({"codigo": cod, "fuente": c["fuente"],
                               "codigo_sgs": limpio, "proyecto": proy})
        else:
            log.append(f"✗ {cod} [{c['fuente']}] no existe en SGS — descartado")
    if not existentes:
        return None, log, True

    # ── Candado de SUPERVISOR (2026-08-27) ────────────────────────────────────────────────────────────────
    # Cada proyecto SGS tiene su Supervisor (approve_id). Si el correo viene de
    # un supervisor es ilógico liquidar un proyecto de otro supervisor. Un candidato
    # cuyo supervisor en SGS es claramente OTRA persona se descarta — pero solo
    # cuando algún otro candidato SÍ calza con el remitente (si ninguno calza,
    # se conserva el orden y se avisa fuerte, sin descartar).
    if remitente:
        rt = _tokens_nombre_b(remitente)
        try:
            ids = [e["proyecto"]["id"] for e in existentes]
            recs = cliente.search_read("project.dominion", [["id", "in", ids]],
                                       ["approve_id"], limit=50)
            sup_por_id = {}
            for r in recs:
                ap = r.get("approve_id")
                sup_por_id[r["id"]] = ap[1] if isinstance(ap, (list, tuple)) and len(ap) > 1 else ""
        except Exception:
            sup_por_id = {}
        for e in existentes:
            sup = sup_por_id.get(e["proyecto"]["id"], "")
            e["supervisor_sgs"] = sup
            st = _tokens_nombre_b(sup)
            e["sup_match"] = (len(rt & st) >= 2) if (rt and st) else None
        con_match = [e for e in existentes if e["sup_match"] is True]
        if con_match:
            for e in existentes:
                if e["sup_match"] is False:
                    log.append(
                        f"Candado: {e['codigo']} DESCARTADO: su supervisor en SGS es "
                        f"{e['supervisor_sgs']} y el correo viene de {remitente}")
            existentes = [e for e in existentes if e["sup_match"] is not False]
            log.append(f"Candado: Supervisor verificado: {con_match[0]['codigo']} es de "
                       f"{con_match[0]['supervisor_sgs']} = remitente del correo")
        elif any(e["sup_match"] is False for e in existentes):
            log.append(
                f"⚠ OJO: ningún candidato tiene a {remitente} como supervisor en SGS "
                f"({'; '.join(e['codigo'] + '→' + (e['supervisor_sgs'] or '?') for e in existentes[:4])}) "
                "— revisar a mano antes de liquidar")

    objetivo = _expandir_nodos_planilla(nodos_planilla)
    if objetivo and len(existentes) > 1:
        def _hits(e):
            try:
                hijos = cliente.obtener_nodos_hijo(e["proyecto"]["id"]) or []
            except Exception:
                return -1
            nodos_sgs = {_norm_nodo(_nodo_de_nombre_sgs(str(h.get("name") or "")))
                         for h in hijos if isinstance(h, dict)}
            nodos_sgs |= {_norm_nodo(str(h.get("nodo") or ""))
                          for h in hijos if isinstance(h, dict)}
            return len(objetivo & nodos_sgs)

        # Regla (2026-08-27): la coherencia de nodos
        # JAMÁS pisa un código dicho EXPLÍCITAMENTE por el supervisor (planilla
        # del sistema / asunto / cuerpo reciente). Los nodos genéricos (3, 4)
        # existen en media SGS y un proyecto nuevo puede no mostrar nodos aún —
        # la coherencia solo ARBITRA entre candidatos de baja confianza
        # (cadena/tabla/excel/imagen) y, sobre uno explícito, solo AVISA.
        fuente_top = (existentes[0].get("fuente") or "").split(" ")[0]
        if fuente_top in ("planilla", "asunto", "cuerpo"):
            h0 = _hits(existentes[0])
            if h0 == 0:
                log.append(
                    f"⚠ {existentes[0]['codigo']} viene del {fuente_top} (explícito) "
                    f"pero SGS no le muestra los nodos {', '.join(sorted(objetivo))} — "
                    "se respeta igual; verificar en SGS si los nodos aún no existen")
            elif h0 > 0:
                existentes[0]["coherencia_nodos"] = True
                log.append(f"⚖ Coherencia de nodos confirma {existentes[0]['codigo']} "
                           f"({h0}/{len(objetivo)} nodos)")
            return existentes[0], log, True

        mejor, mejor_hits = None, 0
        for e in existentes:
            hits = _hits(e)
            if hits > mejor_hits:
                mejor, mejor_hits = e, hits
        if mejor is not None:
            mejor["coherencia_nodos"] = True
            if mejor is not existentes[0]:
                log.append(
                    f"⚖ COHERENCIA DE NODOS: {mejor['codigo']} contiene {mejor_hits} de "
                    f"{len(objetivo)} nodos de la planilla y {existentes[0]['codigo']} no "
                    f"los tiene (¿encabezado RG11/cadena desactualizados?) — gana "
                    f"{mejor['codigo']}")
            else:
                log.append(f"⚖ Coherencia de nodos confirma {mejor['codigo']} "
                           f"({mejor_hits}/{len(objetivo)} nodos de la planilla)")
            return mejor, log, True
        log.append("⚠ Ninguno de los códigos validados contiene los nodos de la "
                   "planilla — se usa el de mayor confianza; revisar a mano")
    return existentes[0], log, True


# ----------------------------------- filtro anti-doble-liquidación (solo lectura)

def _nodo_de_nombre_sgs(nombre: str) -> str:
    """'DF...-CD55691-NODO1' → '1' · '... NODO 25A' → '25A' ·
    '6301021138 N4' → '4' (caso real 27-ago: hijos nombrados 'N3'/'N4' sin la
    palabra NODO hacían invisible el proyecto para la coherencia de nodos)."""
    up = (nombre or "").upper()
    m = re.search(r"NODO\s*[-_]?\s*([0-9]+[A-Z]?)", up)
    if m:
        return m.group(1)
    m = re.search(r"(?:^|[\s_-])N\s?([0-9]+[A-Z]?)(?:$|[\s_-])", up)
    return m.group(1) if m else ""


def _norm_nodo(n) -> str:
    s = re.sub(r"\s+", "", str(n).upper()).replace("NODO", "")
    s = re.sub(r"^N(?=\d)", "", s)
    s = re.sub(r"[-_]N(?=\d)", "-", s)
    return s


def verificar_liquidacion_previa(codigo: str, filas_mo=None, nodos_filtro=None):
    """¿Este proyecto YA tiene Mano de Obra liquidada en SGS? (solo lectura)

    Semántica de liquidador2 (SGS UI): la liquidación vive en
    `project.activities` — advance > 0 = ya liquidado (total si balance ≈ 0).
    Matiz clave: en Programados con PI mensual es NORMAL que otros nodos tengan
    avance de PIs anteriores; el conflicto real es una actividad con avance que
    choca con lo que estamos por liquidar (mismo QP y, si hay filtro de nodos,
    mismo nodo).

    Devuelve (resumen, None) o (None, error). resumen:
      {"actividades": N, "con_avance_total": M,
       "conflictos": [{"qp","nodo","advance","balance"}], ...}
    """
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, f"SGS no disponible: {str(e)[:80]}"

    cod = (codigo or "").upper()
    try:
        proy, _metodo = _buscar_proyecto_flexible(cliente, cod)
    except Exception as e:
        return None, f"Error buscando el proyecto: {str(e)[:80]}"
    if not proy:
        return None, f"{codigo} no existe en SGS"

    ids = [proy["id"]]
    mapa_nodo = {proy["id"]: ""}
    try:
        for h in (cliente.obtener_nodos_hijo(proy["id"]) or []):
            hid = h.get("id") if isinstance(h, dict) else None
            if hid:
                ids.append(hid)
                mapa_nodo[hid] = _nodo_de_nombre_sgs(str(h.get("name") or ""))
    except Exception:
        pass

    try:
        acts = cliente.search_read(
            "project.activities", [["project_id", "in", ids]],
            ["activity_code", "amount", "advance", "balance", "project_id"],
            limit=800)
    except Exception as e:
        return None, f"Error leyendo project.activities: {str(e)[:80]}"

    qps_objetivo = {str(f.get("codigo") or "").upper() for f in (filas_mo or [])} or None
    filtro = {_norm_nodo(x) for x in (nodos_filtro or [])} or None

    con_avance_total = 0
    conflictos = []
    for a in acts:
        adv = float(a.get("advance") or 0)
        if adv <= 0:
            continue
        con_avance_total += 1
        qp = str(a.get("activity_code") or "").upper()
        pid = a.get("project_id")
        pid = pid[0] if isinstance(pid, (list, tuple)) else pid
        nodo = mapa_nodo.get(pid, "")
        if qps_objetivo is not None and qp not in qps_objetivo:
            continue
        if filtro and nodo and _norm_nodo(nodo) not in filtro:
            continue
        conflictos.append({"qp": qp, "nodo": nodo or "?", "advance": adv,
                           "balance": float(a.get("balance") or 0)})

    return {
        "actividades": len(acts),
        "con_avance_total": con_avance_total,
        "conflictos": conflictos,
    }, None


def lideres_de_bandejas(codigo: str, nodos_filtro=None):
    """Líderes reales desde SGS (solo lectura): el partner de las bandejas de
    materiales de cada nodo del proyecto — misma lógica probada de
    nuevo/api_lideres_sugeridos ("cada bandeja tiene el partner que recibió los
    materiales = el líder real"). Cédula vía res.partner.vat (formato C7916...).
    Devuelve ([{cedula, nombre}], None) o (None, error)."""
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, f"SGS no disponible: {str(e)[:80]}"
    proy, _m = _buscar_proyecto_flexible(cliente, codigo)
    if not proy:
        return None, f"{codigo} no existe en SGS"

    ids = [proy["id"]]
    mapa_nodo = {proy["id"]: ""}
    try:
        for h in (cliente.obtener_nodos_hijo(proy["id"]) or []):
            hid = h.get("id") if isinstance(h, dict) else None
            if hid:
                ids.append(hid)
                mapa_nodo[hid] = _nodo_de_nombre_sgs(str(h.get("name") or ""))
    except Exception:
        pass

    if nodos_filtro:
        filtro = {_norm_nodo(x) for x in nodos_filtro}
        sel = [i for i in ids if mapa_nodo.get(i) and _norm_nodo(mapa_nodo[i]) in filtro]
        if sel:
            ids = sel

    try:
        trays = cliente.search_read("project.tray", [["project_id", "in", ids]],
                                    ["partner_id", "type"], limit=400)
    except Exception as e:
        return None, f"Error leyendo bandejas: {str(e)[:80]}"

    partners = {}
    for t in trays:
        p = t.get("partner_id")
        if isinstance(p, (list, tuple)) and len(p) >= 2 and p[0]:
            partners[p[0]] = p[1]
    if not partners:
        return [], None

    det, con_vat = [], set()
    try:
        for p in cliente.search_read("res.partner", [["id", "in", list(partners)]],
                                     ["name", "vat"], limit=60):
            ced = str(p.get("vat") or "").strip()
            if ced[:1] in "Cc" and ced[1:].isdigit():
                ced = ced[1:]
            det.append({"cedula": ced if ced.isdigit() else "",
                        "nombre": p.get("name") or partners.get(p["id"], "")})
            con_vat.add(p["id"])
    except Exception:
        pass
    for pid, nombre in partners.items():
        if pid not in con_vat:
            det.append({"cedula": "", "nombre": nombre})
    return det, None


def cruce_materiales(codigo: str, nodos_objetivo=None):
    """Pre-análisis MAT (2026-08-27): balance de materiales SGS por
    nodo, para que el operador compare la planilla FÍSICA del supervisor contra lo
    que muestra SGS (despachos, DEVOLUCIONES, neto, consumido) ANTES de
    aprobar que la planilla lleve materiales. Solo lectura. Reutiliza
    _cruce_nodo/_dom_a_sap de nuevo/ como librería.
    Devuelve ([{nodo, pro, filas: [{cod, desc, despachos, devoluciones, neto,
    consumido, estado, bandejas}], devoluciones}], None) o (None, error)."""
    from generar_balance import _cruce_nodo
    from manual_balance import _dom_a_sap, _label_nodo
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, f"SGS no disponible: {str(e)[:80]}"
    try:
        proy, _m = _buscar_proyecto_flexible(cliente, (codigo or "").upper())
    except Exception as e:
        return None, f"Error buscando el proyecto: {str(e)[:80]}"
    if not proy:
        return None, f"{codigo} no existe en SGS"
    try:
        balance = cliente.balance_completo(proy["id"])
    except Exception as e:
        return None, f"Error leyendo balance de materiales: {str(e)[:100]}"

    objetivo = _expandir_nodos_planilla(nodos_objetivo) if nodos_objetivo else set()
    out = []
    for nodo in (balance.get("nodos") or []):
        etiqueta = (_norm_nodo(str(nodo.get("nodo") or ""))
                    or _norm_nodo(_nodo_de_nombre_sgs(str(nodo.get("name") or ""))))
        if objetivo and etiqueta not in objetivo:
            continue
        sap = _dom_a_sap(nodo)
        filas, total_dev = [], 0.0
        try:
            cruce = _cruce_nodo(nodo)
        except Exception:
            cruce = []

        def _fmt_bandejas(lista):
            # [(ban_name, qty), ...] → "BAN-118994×2, BAN-119625×1"
            tot = {}
            for item in (lista or []):
                try:
                    nombre, q = str(item[0]), float(item[1] or 0)
                except Exception:
                    nombre, q = str(item), 0.0
                tot[nombre] = tot.get(nombre, 0.0) + q
            return ", ".join(f"{n}×{q:g}" if q else n for n, q in tot.items())

        for r in cruce:
            filas.append({
                "cod": sap.get(r["cod"]) or r["cod"],
                "desc": r.get("desc", ""),
                "despachos": r.get("tray", 0),
                "devoluciones": r.get("devoluciones", 0),
                "neto": r.get("desp_neto", 0),
                "consumido": r.get("consumido", 0),
                "estado": r.get("estado", ""),
                "ban_desp": _fmt_bandejas(r.get("ban_delivers")),
                "ban_dev": _fmt_bandejas(r.get("ban_devs")),
            })
            total_dev += r.get("devoluciones") or 0
        out.append({"nodo": _label_nodo(nodo.get("nodo")) or etiqueta,
                    "pro": nodo.get("code", ""),
                    "filas": filas, "devoluciones": total_dev})
    return out, None


def clave_nodo_cmp(n) -> str:
    """Clave comparable de nodo, CONSERVANDO los tramos/cables:
    'NODO 4'→'4' · 'N20-N24'→'20_24' · '13AL19'/'13_19'/'N13_N19'→'13_19' ·
    '10A'→'10A'. (_norm_nodo y la coherencia de nodos colapsan cables; aquí
    no, porque N20-N24 y el nodo 20 son proyectos SGS distintos.)"""
    s = re.sub(r"\s+", "", str(n or "").upper()).replace("NODO", "")
    s = re.sub(r"(?<=\d)AL(?=N?\d)", "_", s)
    s = s.replace("-", "_")
    s = re.sub(r"(^|_)N(?=\d)", r"\1", s)
    return s.strip("_")


def nodos_sgs_proyecto(codigo: str):
    """Nodos hijo reales del proyecto en SGS (solo lectura), con su PDL.
    Devuelve ({clave_nodo_cmp: {"name", "pdl"}}, None) o (None, error)."""
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, f"SGS no disponible: {str(e)[:80]}"
    try:
        proy, _m = _buscar_proyecto_flexible(cliente, (codigo or "").upper())
    except Exception as e:
        return None, f"Error buscando el proyecto: {str(e)[:80]}"
    if not proy:
        return None, f"{codigo} no existe en SGS"
    try:
        hijos = cliente.search_read("project.dominion", [["parent_id", "=", proy["id"]]],
                                    ["id", "name", "nodo", "pdl"], limit=500)
    except Exception as e:
        return None, f"Error leyendo nodos: {str(e)[:80]}"
    out = {}
    for h in hijos or []:
        nombre = str(h.get("name") or "")
        clave = clave_nodo_cmp(h.get("nodo")) if h.get("nodo") else ""
        if not clave:
            m = re.search(r"\bN(\d+[A-Z]?)[_-]N?(\d+[A-Z]?)\s*$", nombre.upper())
            clave = f"{m.group(1)}_{m.group(2)}" if m else clave_nodo_cmp(_nodo_de_nombre_sgs(nombre))
        if clave:
            # Nodos recién creados traen el PDL vacío o como guion '-': eso NO
            # es "otro PDL" (falso aviso de "varios PDL", 2-oct).
            pdl_h = str(h.get("pdl") or "").strip().upper()
            out[clave] = {"name": nombre, "pdl": pdl_h if pdl_h.startswith("CDOPP") else ""}
    return out, None


def buscar_lideres_por_nombre(nombre: str):
    """Candidatos de líder en TODO el directorio res.partner de SGS para un
    nombre tecleado a mano (regla 2026-08-24: si el líder no está en
    las bandejas del proyecto, buscar el mejor candidato posible por nombre —
    el nombre puede venir con typos, p. ej. CAMCHO por CAMACHO, así que se
    busca por cada palabra y el que puntúa decide el que llama). Solo partners
    con cédula. Devuelve ([{cedula, nombre}], None) o (None, error)."""
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, f"SGS no disponible: {str(e)[:80]}"
    tokens = [t for t in re.split(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ0-9]+", str(nombre or ""))
              if len(t) >= 4]
    vistos, out = set(), []
    for t in tokens:
        try:
            for p in cliente.search_read("res.partner", [["name", "ilike", t]],
                                         ["name", "vat"], limit=80):
                if p["id"] in vistos:
                    continue
                vistos.add(p["id"])
                ced = str(p.get("vat") or "").strip()
                if ced[:1] in "Cc" and ced[1:].isdigit():
                    ced = ced[1:]
                if ced.isdigit():
                    out.append({"cedula": ced, "nombre": p.get("name") or ""})
        except Exception:
            continue
    return out, None


def fecha_sugerida_bandejas(codigo: str, nodos_filtro=None):
    """Fecha SUGERIDA de ejecución (regla 2026-08-20): la `date` más
    reciente de las bandejas de despacho APROBADAS por almacén entre los nodos
    que se van a liquidar. Es solo sugerencia — el operador la aprueba en la UI.
    Devuelve ({fecha, bandeja, nodo, aprobadas}, None) o (None, error)."""
    try:
        cliente = _sgs_cliente()
    except Exception as e:
        return None, f"SGS no disponible: {str(e)[:80]}"
    proy, _m = _buscar_proyecto_flexible(cliente, codigo)
    if not proy:
        return None, f"{codigo} no existe en SGS"

    ids = [proy["id"]]
    mapa_nodo = {proy["id"]: ""}
    try:
        for h in (cliente.obtener_nodos_hijo(proy["id"]) or []):
            hid = h.get("id") if isinstance(h, dict) else None
            if hid:
                ids.append(hid)
                mapa_nodo[hid] = _nodo_de_nombre_sgs(str(h.get("name") or ""))
    except Exception:
        pass
    if nodos_filtro:
        filtro = {_norm_nodo(x) for x in nodos_filtro}
        sel = [i for i in ids if mapa_nodo.get(i) and _norm_nodo(mapa_nodo[i]) in filtro]
        if sel:
            ids = sel

    try:
        trays = cliente.search_read(
            "project.tray", [["project_id", "in", ids]],
            ["name", "type", "state", "state_picking", "write_date", "project_id"],
            limit=400)
    except Exception as e:
        return None, f"Error leyendo bandejas: {str(e)[:80]}"

    # Semántica verificada con datos reales (20-ago): el campo `date` de la
    # bandeja es PROYECTADO (puede estar en el futuro incluso atendida) — no
    # sirve. La aprobación real: state='approved' escribe la bandeja (su
    # write_date queda en ese momento) y `state_picking='atendido'` confirma
    # que almacén ya despachó físicamente. stock.picking y mail.message están
    # cerrados para este usuario API, así que write_date es el mejor dato real.
    atendidas = [t for t in trays
                 if str(t.get("type")) == "deliver"
                 and str(t.get("state", "")).lower() == "approved"
                 and str(t.get("state_picking", "")).lower() == "atendido"
                 and t.get("write_date")]
    if not atendidas:
        return None, ("Ninguna bandeja de despacho ATENDIDA por almacén aún "
                      "en esos nodos — sin despacho real no hay fecha que sugerir")

    mejor = max(atendidas, key=lambda t: str(t["write_date"]))
    pid = mejor.get("project_id")
    pid = pid[0] if isinstance(pid, (list, tuple)) else pid
    return {
        "fecha": str(mejor["write_date"])[:10],
        "bandeja": str(mejor.get("name") or ""),
        "nodo": mapa_nodo.get(pid, ""),
        "aprobadas": len(atendidas),
    }, None


# ------------------------------------------------------ puente hacia nuevo/

def crear_sesion_nuevo(candidato: dict, info: dict = None):
    """Crea una sesión pre-llenada en el wizard de nuevo/ a partir de un
    candidato ya extraído (ver escanear_correos_orden_pendiente). Reutiliza
    los módulos de nuevo/ como librería — no toca Flask ni HTTP, no modifica
    ningún archivo de ese proyecto. Devuelve (sid, error).

    info (opcional): dict de salida — se anota mat_nodos / mat_error para que
    el llamador pueda informar qué pasó con los materiales."""
    from integracion import cfg_desde_archivo
    from sgs_client import SGSClient
    from manual_balance import nueva_sesion, save_sesion, get_sesion, parsear_mo, enriquecer_mo

    codigo_raw = (candidato.get("codigo") or "").strip()
    if not codigo_raw:
        return None, "No se detectó ningún código DF/LCL en este correo — revísalo a mano"

    codigo = codigo_raw.upper()
    if codigo.startswith("LCL"):
        codigo = codigo[3:].lstrip("_- ")

    # ── Ninguna prestación se pierde en silencio (caso real 2-oct: 11 de 42
    # filas —$49M de $57M— rechazadas por nodo inválido y la planilla salió
    # con 31 sin avisar). Se valida ANTES de crear la sesión; si nuevo/
    # rechazaría alguna fila, no se genera una planilla incompleta.
    filas_in = candidato.get("filas_mo") or []
    if filas_in:
        _txt = "\n".join(f"{f['nodo']}\t{f['codigo']}\t{f['cantidad']}" for f in filas_in)
        _rows_ok, _errs = parsear_mo(_txt)
        if isinstance(info, dict):
            info["mo_entrada"] = len(filas_in)
            info["mo_salida"] = len(_rows_ok)
        if _errs:
            malas = []
            for e in _errs:
                mm = re.match(r"Fila (\d+):", e)
                if mm and 1 <= int(mm.group(1)) <= len(filas_in):
                    f = filas_in[int(mm.group(1)) - 1]
                    malas.append(f"{f['codigo']}×{f['cantidad']:g} (nodo '{f['nodo']}')")
                else:
                    malas.append(e)
            return None, (f"✗ {len(_errs)} de {len(filas_in)} prestaciones NO entrarían a la "
                          f"planilla por nodo/código inválido: {'; '.join(malas[:8])}"
                          + ("…" if len(malas) > 8 else "")
                          + " — no se genera una planilla incompleta")

    cfg = cfg_desde_archivo()
    try:
        cliente = SGSClient(cfg["sgs_url"], cfg["sgs_db"], cfg["sgs_login"], cfg["sgs_password"])
        cliente.autenticar()
    except Exception as e:
        return None, f"No se pudo conectar a SGS: {e}"

    try:
        proyecto, _metodo = _buscar_proyecto_flexible(cliente, codigo_raw.upper())
    except Exception as e:
        return None, f"Error consultando SGS: {e}"
    if not proyecto:
        return None, f"SGS no encontró ningún proyecto con código '{codigo_raw}'"

    parent_id = proyecto["id"]
    padre = cliente.leer_proyecto(parent_id)
    sid = nueva_sesion(padre or proyecto, parent_id)

    s = get_sesion(sid)
    # Regla de negocio: MI (DF) no lleva PDL; solo se guarda si el análisis
    # encontró un PDL/identificador aparte (típico en Programados).
    s["pdl"] = candidato.get("pdl") or ""
    # order_number basura (caso real DF202664645116 con order_number='0'): el
    # lcl_odm saldría '0' en el nombre del archivo y en el metadato OdM/LCL de
    # la planilla, y liquidador2 no resolvería el proyecto. Usar el código real.
    if len(str(s.get("lcl_odm") or "").strip()) < 8:
        s["lcl_odm"] = codigo

    filas = candidato.get("filas_mo") or []
    if filas:
        texto = "\n".join(f"{f['nodo']}\t{f['codigo']}\t{f['cantidad']}" for f in filas)
        rows, errs_parse = parsear_mo(texto)
        enriquecer_mo(rows)
        s["rows_mo"] = rows
        s["errs_mo"] = errs_parse

    # ── Materiales (2026-08-27: el embudo deja de ser solo MO) ────────────────
    # liquidador2 liquida MAT leyendo las hojas N<nodo> (balance de bandejas de
    # SGS). Regla de oro de SGS UI: la cantidad autorizada de un material sale
    # de SGS (neto de bandeja − avance real), NUNCA del Excel del supervisor —
    # por eso aquí solo se puebla sesion["nodos_mat"] con los nodos SGS que se
    # van a liquidar y generar_excel_manual escribe las hojas. Solo lectura;
    # si SGS falla, la planilla sigue siendo válida (solo MO).
    s.setdefault("nodos_mat", [])
    s.setdefault("nodos_sel", [])
    s["modo_mat"] = s.get("modo_mat") or "nodo_solo"
    if candidato.get("incluir_mat", True) and filas:
        try:
            # Si el operador aprobó nodos concretos en el pre-análisis, mandan esos;
            # si no, los nodos de las filas de MO.
            sel_aprobados = candidato.get("mat_nodos_sel") or []
            objetivo = _expandir_nodos_planilla(
                sel_aprobados or [f["nodo"] for f in filas])
            balance = cliente.balance_completo(parent_id)
            nodos_sgs = balance.get("nodos") or []
            sel = [n for n in nodos_sgs
                   if _norm_nodo(str(n.get("nodo") or "")) in objetivo
                   or _norm_nodo(_nodo_de_nombre_sgs(str(n.get("name") or ""))) in objetivo]
            s["nodos_mat"] = sel
            s["nodos_sel"] = [n.get("code") for n in sel if n.get("code")]
            if isinstance(info, dict):
                info["mat_nodos"] = len(sel)
        except Exception as e:
            if isinstance(info, dict):
                info["mat_error"] = str(e)[:120]
    save_sesion(sid)

    # A propósito NO se lanza iniciar_fetch_sgs aquí: la URL de entrada es
    # /manual/retomar/<sid>, que relanza el fetch dentro del proceso del
    # servidor de nuevo/. Lanzarlo también desde este proceso sería redundante
    # y dejaría datos a medias si este proceso muere antes de terminar.
    return sid, None
