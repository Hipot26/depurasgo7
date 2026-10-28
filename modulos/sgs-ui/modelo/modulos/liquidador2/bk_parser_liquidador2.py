"""
parser_liquidador2.py — Copia aislada de parser_excel.py para Liquidador2.
Mismo reconocimiento de documentos, plantillas y reglas de extracción que el
liquidador raíz. Modificar aquí sin afectar el liquidador original.
Solo lectura de archivos locales. No toca SGS.
"""

import datetime
import json
import re
import unicodedata

# Hoja de materiales del Balance: nombre vigente y nombre heredado (archivos ya generados).
HOJAS_CRUCE = ("CRUCE_LIQUIDADOR", "CRUCE_SERGIO")
HOJAS_CRUCE_MAT = HOJAS_CRUCE + ("CRUCE_MAT",)

import openpyxl
import requests


def _minimax_extraer_nodo(texto_header: str) -> str | None:
    """
    Fallback SOLO para cuando el regex determinista no reconoce el formato del
    header de sección ("Nodo: ..."). En SGS ese texto se escribe a mano y el
    formato varía sin aviso (individual, rango con 'AL', con guion/underscore,
    en palabras, etc.) — un regex fijo nunca cubre todas las variantes posibles.

    Nunca se usa si el regex ya extrajo algo: el regex tiene prioridad porque es
    determinista y gratis. Si MiniMax tampoco puede interpretar el texto,
    devuelve None y el caller debe omitir la sección (mejor perder unas filas
    que asignarlas al nodo equivocado).
    """
    # Import diferido: evita duplicar la API key como segundo literal en el
    # codigo fuente (reusa la unica constante ya definida en blueprint.py).
    # No es circular porque solo se resuelve cuando la funcion se ejecuta en
    # un request, momento en que blueprint.py ya terminó de cargar.
    try:
        from modulos.liquidador2.blueprint import MINIMAX_API_KEY
    except ImportError:
        return None

    prompt = (
        "Eres un parser estricto para reportes de Dominion Colombia (SGS). El "
        "encabezado de una sección de materiales identifica un NODO, que puede "
        "ser individual (ej. '18') o un rango/cable entre dos nodos escrito de "
        "formas variadas a mano (ej. '18AL23', '18 AL 23', 'N18_N23', 'ND18-23', "
        "'nodo cable 18 y 23', 'del 18 al 23', 'tramo 18-23').\n\n"
        f"Texto del encabezado: {texto_header!r}\n\n"
        "Extrae el identificador de nodo EXACTO según aparece (preserva TODOS los "
        "números si es un rango, en el orden en que aparecen). Responde SOLO JSON, "
        'sin markdown: {"nodo": "18AL23"} o {"nodo": null} si el texto no contiene '
        "un nodo reconocible."
    )
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": "MiniMax-Text-01",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 100,
        "temperature": 0.0,
    }
    for endpoint in [
        "https://api.minimax.io/v1/text/chatcompletion_v2",
        "https://api.minimax.chat/v1/text/chatcompletion_v2",
    ]:
        try:
            resp = requests.post(endpoint, headers=headers, json=body, timeout=12)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("choices"):
                continue
            content = (data["choices"][0].get("message") or {}).get("content") or ""
            content = content.strip()
            if "```" in content:
                parts = content.split("```")
                content = parts[1] if len(parts) > 1 else content
                if content.lower().startswith("json"):
                    content = content[4:]
            parsed = json.loads(content.strip())
            nodo = parsed.get("nodo")
            return str(nodo).strip() or None if nodo else None
        except Exception:
            continue
    return None

# ── Hojas y marcadores ────────────────────────────────────────────────────────
_HOJAS_PRIORITARIAS_MO = [
    "SIP_MANO_OBRA", "MANO_OBRA", "MO_MANUAL",
    "LIQUIDACION", "LIQ", "PRESTACIONES", "ACTIVIDADES",
]
_MARCADORES_PRO   = {"PRO", "PROYECTO", "PROJECT", "ODM", "NODO_PRO", "NODO"}
_MARCADORES_COD   = {"CODIGO", "COD", "CODE", "PRESTACION", "ACTIVIDAD", "ACTIVITY"}
_MARCADORES_QTY   = {"CANTIDAD", "CANT", "QTY", "AMOUNT", "UNIDADES", "TOTAL"}
_MARCADORES_LIDER = {"LIDER", "RESPONSABLE", "PARTNER", "TECNICO"}
_EXCLUIR          = {"DESC", "NOMBRE", "OBSERV", "NOTA"}


def _sin_tildes(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )


def _col_match(v: str, marcadores: set, excluir: set = None) -> bool:
    v_norm = _sin_tildes(v)
    if excluir and any(e in v_norm for e in excluir):
        return False
    return any(m in v_norm for m in marcadores)


def _parse_float(val) -> float | None:
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
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")
        else:
            s = s.replace(".", "").replace(",", ".")
    elif has_comma:
        partes = s.split(",")
        if len(partes) == 2 and len(partes[1]) <= 2:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif has_dot and s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _normalizar_codigo_proyecto(val) -> str:
    raw = str(val or "").strip().upper()
    if not raw or raw in {"0", "N/A", "NA", "-", "NONE"}:
        return ""
    m = re.search(r"\bLCL\s*[-_:]?\s*(6300\d+)\b", raw)
    if m:
        return m.group(1)
    m = re.search(r"\b(6300\d{4,})\b", raw)
    if m:
        return m.group(1)
    m = re.search(r"\b(DF[\w\d]+)\b", raw)
    if m:
        return m.group(1)
    # Marcadores de "aún no asignado" NO son códigos de proyecto. Caso real
    # DF202664650522 (2026-09-07): SIP traía "LCL: PENDIENTE" y el verificador
    # buscó el DF 'PENDIENTE' en SGS → nodos existentes reportados como faltantes.
    # Un código de proyecto siempre lleva al menos un dígito.
    if not re.search(r"\d", raw):
        return ""
    return raw


def _normalizar_pdl(val) -> str:
    raw = str(val or "").strip().upper()
    if not raw or raw in {"-", "0", "N/A", "NA", "NONE"}:
        return ""
    # Acepta CDOPPI202678029 y CDOPP2026R04432 (con o sin "I" intermedia).
    # Un campo puede tener varios PDL separados por ; o , — conservar todos.
    ms = re.findall(r"\b(CDOPP[I]?[\w\d-]+)\b", raw)
    if ms:
        return ";".join(ms)
    # 'PDL: DF202664490809' embebido en una cabecera larga (hojas N<nodo> v2):
    # extraer solo el código, nunca devolver la fila completa.
    m = re.search(r"PDL\s*[:\-]\s*([A-Z0-9][\w-]*)", raw)
    if m:
        return m.group(1)
    m = re.search(r"\b(DF\s?\d[\w\d-]*)\b", raw)
    if m:
        return m.group(1).replace(" ", "")
    return raw


# ── Parser MO ─────────────────────────────────────────────────────────────────
def parsear_mo(wb: openpyxl.Workbook) -> tuple[list, list]:
    """
    Extrae filas de mano de obra.
    Devuelve (rows, errores).
    Cada row: {fila, pro, codigo, cantidad, lider, _odm}
    """
    rows, errs = [], []

    ws = None
    for nombre in _HOJAS_PRIORITARIAS_MO:
        if nombre in wb.sheetnames:
            ws = wb[nombre]
            break
    if ws is None:
        ws = wb.active

    col = {"pro": None, "cod": None, "qty": None, "lider": None}
    header_row = None

    for r in range(1, 21):
        vals = [str(ws.cell(r, c).value or "").upper().strip() for c in range(1, 25)]
        row_col = {"pro": None, "cod": None, "qty": None, "lider": None}
        for i, v in enumerate(vals):
            if not row_col["pro"]   and _col_match(v, _MARCADORES_PRO):
                row_col["pro"]   = i + 1
            if not row_col["cod"]   and _col_match(v, _MARCADORES_COD, _EXCLUIR):
                row_col["cod"]   = i + 1
            if not row_col["qty"]   and _col_match(v, _MARCADORES_QTY, _EXCLUIR):
                row_col["qty"]   = i + 1
            if not row_col["lider"] and _col_match(v, _MARCADORES_LIDER):
                row_col["lider"] = i + 1
        if row_col["cod"] and row_col["qty"]:
            col        = row_col
            header_row = r
            break

    if not header_row:
        errs.append("No se detectaron columnas requeridas (CODIGO + CANTIDAD).")
        return rows, errs

    # ODM/DF/LCL del bloque de metadatos
    odm_meta = ""
    if ws.title in ("SIP_MANO_OBRA", "MANO_OBRA", "MO_MANUAL"):
        # Programados: preferir LCL/6300 sobre el PDL CDOPPI.
        # Mayor Impacto: si no hay LCL valido, conserva el DF/PDL.
        # MO_MANUAL: cabecera "OdM / LCL:" en col 1 contiene "LCL" → se extrae igual.
        # Fallback ("PROYECTO","PADRE"): balance_sgs_manual trae el DF en la fila
        # 'Proyecto:' y el PRO en 'Código padre:' — sin esto _odm queda vacío y el
        # nodo numérico se confunde con un id de Odoo (caso PRO-000001 'Prueba').
        for grupo in [("LCL",), ("TICKET", "PDL"), ("ODM", "GRAFO"), ("PROYECTO", "PADRE")]:
            for r_m in range(1, min(header_row + 1, 20)):
                label = _sin_tildes(str(ws.cell(r_m, 1).value or "").upper().strip())
                if any(lbl in label for lbl in grupo):
                    val = _normalizar_codigo_proyecto(ws.cell(r_m, 2).value)
                    if val:
                        odm_meta = val
                        break
            if odm_meta:
                break
        if odm_meta:
            errs.append(f"[INFO] Modo SIP_MANO_OBRA — DF/LCL: {odm_meta}")

    vistos = set()
    for r in range(header_row + 1, ws.max_row + 1):
        pro     = str(ws.cell(r, col["pro"]).value  or "").strip().upper() if col["pro"]   else ""
        cod     = str(ws.cell(r, col["cod"]).value  or "").strip().upper()
        lid     = str(ws.cell(r, col["lider"]).value or "").strip()        if col["lider"] else ""
        qty_raw = ws.cell(r, col["qty"]).value

        if not cod or qty_raw is None:
            continue
        try:
            qty = float(qty_raw)
        except (TypeError, ValueError):
            continue
        if qty == 0:
            continue

        clave = (pro or odm_meta, cod)
        if clave in vistos:
            errs.append(f"Fila {r}: duplicado ({pro or odm_meta}, {cod}) — ignorado")
            continue
        vistos.add(clave)

        rows.append({
            "fila":     r,
            "pro":      pro,
            "codigo":   cod,
            "cantidad": qty,
            "lider":    lid,
            "_odm":     odm_meta,
        })

    return rows, errs


def extraer_total_mo(wb: openpyxl.Workbook) -> float | None:
    """Lee el total de mano de obra del Excel (fila TOTAL MANO DE OBRA)."""
    for hoja in ("SIP_MANO_OBRA", "MANO_OBRA", "MO_MANUAL"):
        if hoja not in wb.sheetnames:
            continue
        ws = wb[hoja]
        for r in range(1, ws.max_row + 1):
            label = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
            if "TOTAL" in label and ("MANO" in label or "OBRA" in label):
                vals = [_parse_float(ws.cell(r, c).value) for c in range(2, 15)]
                vals = [v for v in vals if v and v > 0]
                if vals:
                    return max(vals)
    return None


def extraer_pdl_programado(wb: openpyxl.Workbook) -> str:
    """
    Extrae el valor de Ticket / PDL desde la cabecera del Excel SIP, SGS_BALANCE o N<nodo>.
    Ejemplo esperado: CDOPPI202678029.
    """
    # Hojas clásicas: buscar fila con "TICKET" o "PDL" en col1, valor en col2.
    # MO_MANUAL: plantilla manual (balance_sgs_manual) — cabecera 'PDL:' en col1.
    for hoja in ("SIP_MANO_OBRA", "MANO_OBRA", "MO_MANUAL", "SGS_BALANCE"):
        if hoja not in wb.sheetnames:
            continue
        ws = wb[hoja]
        for r in range(1, min(ws.max_row + 1, 20)):
            label = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
            if "TICKET" in label or "PDL" in label:
                pdl = _normalizar_pdl(ws.cell(r, 2).value)
                if pdl:
                    return pdl

    # Hojas N<nodo> v2: PDL embebido en R1 (e.g. "Nodo 68 — PRO-053414 | ... | PDL: CDOPPI202679493")
    for sheet_name in wb.sheetnames:
        if not re.match(r"^N\d+[A-Z]?(?:_N\d+[A-Z]?)*$", sheet_name):
            continue
        ws = wb[sheet_name]
        for c in range(1, 10):
            cell_val = str(ws.cell(1, c).value or "")
            if "PDL" in cell_val.upper():
                pdl = _normalizar_pdl(cell_val)
                if pdl:
                    return pdl
        break  # todas las hojas comparten el mismo PDL; una basta

    # Fallback genérico: cualquier hoja con etiqueta PDL/TICKET (palabra completa)
    # en col1 y el código en col2 — cubre plantillas manuales futuras sin tener
    # que registrar cada nombre de hoja. Palabra completa para no capturar
    # etiquetas que solo contengan la subcadena.
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for r in range(1, min(ws.max_row + 1, 20)):
            label = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
            if re.search(r"\b(?:PDL|TICKET)\b", label):
                pdl = _normalizar_pdl(ws.cell(r, 2).value)
                if pdl:
                    return pdl

    return ""


# ── Parser MAT ────────────────────────────────────────────────────────────────
def _parsear_mat_nodo_sheets(wb: openpyxl.Workbook, errs: list) -> list:
    """
    Extrae materiales de hojas N<nodo> (balance_sgs_manual v2).
    Cada hoja = un nodo: R1=cabecera, R2=columnas, R3..=datos, última=subtotal.
    Usa códigos SAP y el campo Estado ya calculado en el Excel.
    """
    nodo_sheet_names = sorted(
        [s for s in wb.sheetnames if re.match(r"^N\d+[A-Z]?(?:_N\d+[A-Z]?)*$", s)],
        key=lambda s: int(re.search(r"\d+", s).group()),
    )
    all_rows = []
    for sheet_name in nodo_sheet_names:
        ws = wb[sheet_name]
        if ws.max_row < 3:
            continue

        # R1: nodo identifier — para N68 → "68", para N1_N2 → "1_N2", N31A → "31A"
        r1_v1 = _sin_tildes(str(ws.cell(1, 1).value or "").upper().strip())
        m = re.search(r"NODO\s+(\d+[A-Z]?(?:[_\-]N?\d+[A-Z]?)*)", r1_v1)
        current_nodo = m.group(1) if m else sheet_name[1:]

        # R2: detect columns
        col = {"cod": None, "desc": None, "desp": None, "dev": None, "neto": None, "estado": None}
        for c in range(1, 15):
            v = _sin_tildes(str(ws.cell(2, c).value or "").upper().strip())
            if ("CODIGO" in v or "SAP" in v) and col["cod"]    is None: col["cod"]    = c
            if "DESCRIPCION" in v            and col["desc"]   is None: col["desc"]   = c
            if "DESPACHO" in v               and col["desp"]   is None: col["desp"]   = c
            if "DEVOLUCION" in v             and col["dev"]    is None: col["dev"]    = c
            if "NETO" in v                   and col["neto"]   is None: col["neto"]   = c
            if v == "ESTADO"                 and col["estado"] is None: col["estado"] = c

        if not col["cod"] or not col["neto"]:
            errs.append(f"{sheet_name}: columnas SAP/Neto no detectadas en fila 2.")
            continue

        for r in range(3, ws.max_row + 1):
            # Saltar subtotal
            if "SUBTOTAL" in _sin_tildes(str(ws.cell(r, 1).value or "").upper()):
                continue
            try:
                int(float(str(ws.cell(r, 1).value or "").strip()))
            except (ValueError, TypeError):
                continue

            cod = str(ws.cell(r, col["cod"]).value or "").strip()
            if not cod or not any(ch.isdigit() for ch in cod):
                continue

            desc     = str(ws.cell(r, col["desc"]).value or "").strip()  if col["desc"]   else ""
            neto_raw = ws.cell(r, col["neto"]).value                     if col["neto"]   else None
            desp_raw = ws.cell(r, col["desp"]).value                     if col["desp"]   else None

            sgs_neto = _parse_float(neto_raw) or 0.0
            desp     = _parse_float(desp_raw) or 0.0

            if col["estado"]:
                estado_excel = str(ws.cell(r, col["estado"]).value or "").strip().upper()
            else:
                estado_excel = ""
            if not estado_excel:
                if sgs_neto > 0:
                    estado_excel = "PENDIENTE"
                elif desp > 0:
                    estado_excel = "DEVOLUCION"
                else:
                    estado_excel = "SIN MOV"

            all_rows.append({
                "fila":           r,
                "nodo":           current_nodo,
                "codigo":         cod,
                "descripcion":    desc,
                "sip_cant":       None,
                "sgs_neto_excel": sgs_neto,
                "estado_excel":   estado_excel,
            })

    if not all_rows:
        errs.append("Hojas N<nodo>: no se encontraron filas de materiales.")
    return all_rows


def _parsear_mat_sgsbalance(ws: openpyxl.worksheet.worksheet.Worksheet, errs: list) -> list:
    """
    Extrae materiales de la hoja SGS_BALANCE (formato balance_sgs_manual).
    Usa códigos SAP (col 'Código SAP') que coinciden con project.material.product_code.
    Estructura: secciones por nodo con header repetido, subtotal al final de cada nodo.
    Estado se deriva de Despachos / Devoluciones / Bal. Neto.
    """
    rows = []
    current_nodo     = ""
    current_pro_code = ""
    col = {"cod": None, "desc": None, "desp": None, "dev": None, "neto": None}
    in_data = False

    # Extraer el DF del proyecto padre del encabezado (ej: "Proyecto padre: DF202664431725").
    # Cuando SIP tiene OdM=0/LCL=0, el DF de SIP y el de SGS son distintos; este valor
    # es el identificador real en Odoo y se usará como _odm para resolver el pid.
    sgs_odm = ""
    for r_hdr in range(1, min(ws.max_row + 1, 15)):
        label = _sin_tildes(str(ws.cell(r_hdr, 1).value or "").upper().strip())
        if "PROYECTO" in label and "PADRE" in label:
            sgs_odm = _normalizar_codigo_proyecto(ws.cell(r_hdr, 2).value)
            if sgs_odm:
                errs.append(f"[INFO] SGS_BALANCE — proyecto padre: {sgs_odm}")
                break

    for r in range(1, ws.max_row + 1):
        v1_raw = str(ws.cell(r, 1).value or "").strip()
        v1     = _sin_tildes(v1_raw.upper())

        # Cabecera de sección: "  Nodo: 52  —  PRO-054896", rango cable "Nodo: 18AL23"
        # o nodo alfanumérico "Nodo: 31A". El regex debe capturar el identificador
        # COMPLETO: si "18AL23" se trunca a "18" el resolver busca un nodo individual
        # inexistente, y si "31A" se trunca a "31" las filas se fusionan con el nodo
        # 31 y sus materiales salen "no encontrados" (caso real: 6300996351, nodos
        # 31A/33A → PRO-057387/PRO-057346, 12 materiales reportados como inexistentes).
        if "NODO:" in v1:
            m = re.search(
                r"NODO:\s*(\d+[A-Z]?\s*AL\s*\d+[A-Z]?"   # rango: 18AL23 / 18 AL 23
                r"|\d+[A-Z]?[_\-]N?\d+[A-Z]?"            # cable: 3_N4 / 1-4
                r"|\d+[A-Z]?)",                          # simple: 52 / 31A
                v1)
            # Extraer PRO code cuando el módulo "nuevo" lo embedió: "Nodo: 1  PRO: PRO-057446"
            pro_m = re.search(r'\bPRO:\s*(PRO-\d+)', v1)
            current_pro_code = pro_m.group(1) if pro_m else ""
            if m:
                current_nodo = m.group(1)
            else:
                # Formato atipico que el regex no cubre — MiniMax interpreta
                # el texto crudo. Si tampoco puede, la sección queda sin nodo
                # y sus filas se omiten (mejor omitir que asignar mal).
                nodo_ia = _minimax_extraer_nodo(v1_raw)
                if nodo_ia:
                    errs.append(f"[INFO] Nodo interpretado por IA: '{v1_raw}' -> '{nodo_ia}'")
                    current_nodo = nodo_ia
                else:
                    errs.append(f"[WARN] No se reconoció el nodo en: '{v1_raw}' — sección omitida.")
                    current_nodo = ""
            in_data = False
            col = {"cod": None, "desc": None, "desp": None, "dev": None, "neto": None}
            continue

        # Fila de subtotal → cierra sección
        if "SUBTOTAL" in v1:
            in_data = False
            continue

        # Detectar fila de columnas: col1='#', col2 contiene 'SAP' o 'CODIGO'
        v2 = _sin_tildes(str(ws.cell(r, 2).value or "").upper().strip())
        if v1 == "#" and ("SAP" in v2 or "CODIGO" in v2):
            tmp = {"cod": None, "desc": None, "desp": None, "dev": None, "neto": None}
            vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 12)]
            for i, v in enumerate(vals):
                if ("CODIGO" in v or "SAP" in v) and tmp["cod"]  is None: tmp["cod"]  = i + 1
                if "DESCRIPCION" in v             and tmp["desc"] is None: tmp["desc"] = i + 1
                if "DESPACHO" in v                and tmp["desp"] is None: tmp["desp"] = i + 1
                if "DEVOLUCION" in v              and tmp["dev"]  is None: tmp["dev"]  = i + 1
                if "NETO" in v                    and tmp["neto"] is None: tmp["neto"] = i + 1
            if tmp["cod"] and tmp["neto"]:
                col     = tmp
                in_data = True
            continue

        if not in_data or not current_nodo:
            continue

        # Fila de datos: col1 debe ser entero
        try:
            int(float(str(ws.cell(r, 1).value or "").strip()))
        except (ValueError, TypeError):
            continue

        cod = str(ws.cell(r, col["cod"]).value or "").strip() if col["cod"] else ""
        if not cod or not any(ch.isdigit() for ch in cod):
            continue

        desc     = str(ws.cell(r, col["desc"]).value or "").strip() if col["desc"] else ""
        neto_raw = ws.cell(r, col["neto"]).value if col["neto"] else None
        desp_raw = ws.cell(r, col["desp"]).value if col["desp"] else None

        sgs_neto = _parse_float(neto_raw) or 0.0
        desp     = _parse_float(desp_raw) or 0.0

        if sgs_neto > 0:
            estado_excel = "PENDIENTE"
        elif desp > 0:
            estado_excel = "DEVOLUCION"
        else:
            estado_excel = "SIN MOV"

        rows.append({
            "fila":           r,
            "nodo":           current_nodo,
            "pro_code":       current_pro_code,
            "codigo":         cod,
            "descripcion":    desc,
            "sip_cant":       None,
            "sgs_neto_excel": sgs_neto,
            "estado_excel":   estado_excel,
            "_odm":           sgs_odm,
        })

    if not rows:
        errs.append("SGS_BALANCE: no se encontraron filas de materiales.")
        return rows

    # Deduplicar por (nodo, codigo): SGS_BALANCE puede tener el mismo código
    # en dos filas (regular + custodia con mismo SAP code). Conservar solo la
    # fila con mayor sgs_neto_excel; si empatan, la de mayor despacho.
    seen: dict[tuple, dict] = {}
    for row in rows:
        key = (row["nodo"], row["codigo"])
        prev = seen.get(key)
        if prev is None or row["sgs_neto_excel"] > prev["sgs_neto_excel"]:
            seen[key] = row
    dedup = list(seen.values())
    if len(dedup) < len(rows):
        errs.append(
            f"[INFO] SGS_BALANCE: {len(rows) - len(dedup)} fila(s) duplicada(s) por código omitida(s)."
        )
    return dedup


def extraer_nodos_faltantes_mo(wb: openpyxl.Workbook) -> dict:
    """
    Lee la hoja MO_MANUAL y detecta nodos que todavía no existen en SGS
    (marcados con "PENDIENTE CREAR SGS" en la columna Observaciones).

    Retorna:
    {
        "nodos_faltantes":  ["125", "127AL122", "128AL129"],
        "parent_pro_code":  "PRO-055781",
        "ticket":           "6300995703",
        "lider_nombre":     "APELLIDO1 APELLIDO2 NOMBRE1 NOMBRE2",
    }
    Cuando no aplica (hoja no existe, sin pendientes) retorna listas/strings vacíos.
    """
    resultado = {
        "nodos_faltantes": [],
        "parent_pro_code": "",
        "ticket":          "",
        "lider_nombre":    "",
    }

    ws = None
    for nombre_hoja in ("MO_MANUAL", "MANO_OBRA", "SIP_MANO_OBRA"):
        if nombre_hoja in wb.sheetnames:
            ws = wb[nombre_hoja]
            break
    if ws is None:
        return resultado

    # Metadatos de cabecera (filas 1-15)
    for r in range(1, 16):
        label = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
        val2  = str(ws.cell(r, 2).value or "").strip()
        # "Código padre:" → PRO-XXXXXX
        if ("PADRE" in label or ("CODIGO" in label and "PADRE" in label)):
            m = re.search(r"(PRO-\d+)", val2.upper())
            if m:
                resultado["parent_pro_code"] = m.group(1)
        # "OdM / LCL:" o "Ticket:" → código numérico 63009XXXXX
        elif "OD" in label or "LCL" in label or "TICKET" in label:
            v = _normalizar_codigo_proyecto(val2)
            if v:
                resultado["ticket"] = v

    # Localizar fila de cabecera con columna "NODO" y columna "OBSERV..."
    col_nodo = col_obs = header_row = None
    for r in range(1, 20):
        found_nodo = found_obs = False
        for c in range(1, 15):
            v = _sin_tildes(str(ws.cell(r, c).value or "").upper().strip())
            if v == "NODO" and col_nodo is None:
                col_nodo   = c
                found_nodo = True
            if "OBSERV" in v and col_obs is None:
                col_obs   = c
                found_obs = True
        if found_nodo and found_obs:
            header_row = r
            break

    if header_row and col_nodo and col_obs:
        vistos = set()
        for r in range(header_row + 1, ws.max_row + 1):
            nodo_val = str(ws.cell(r, col_nodo).value or "").strip()
            obs_val  = _sin_tildes(str(ws.cell(r, col_obs).value or "").upper().strip())
            if not nodo_val:
                continue
            if "PENDIENTE" in obs_val and "SGS" in obs_val and nodo_val not in vistos:
                vistos.add(nodo_val)
                resultado["nodos_faltantes"].append(nodo_val)

    # Líder responsable desde sección de distribución
    en_dist = False
    for r in range(1, ws.max_row + 1):
        label = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
        if not en_dist:
            if "DISTRIB" in label and "LIQUID" in label:
                en_dist = True
            continue
        # Primera fila de datos: col2 = cédula (contiene dígitos), col3 = nombre
        cedula = str(ws.cell(r, 2).value or "").strip()
        nombre = str(ws.cell(r, 3).value or "").strip()
        if cedula and nombre and any(ch.isdigit() for ch in cedula):
            resultado["lider_nombre"] = nombre
            break

    return resultado


def extraer_distribucion_mo(wb: openpyxl.Workbook) -> list:
    """
    Lee la lista explícita de líderes con cédula y porcentaje asignado en el
    Excel. Fuentes, en orden de prioridad:
      1. Hoja LIQUIDACION — sección 'LIDERES A LIQUIDAR' (formato nuevo).
      2. MO_MANUAL/MANO_OBRA/SIP_MANO_OBRA — sección 'DISTRIBUCIÓN DE
         LIQUIDACIÓN — LÍDERES RESPONSABLES' (formato anterior).

    Retorna [{cedula: "C79168117", nombre: "CASTILLO GOMEZ...", pct: 25.0}, ...]
    """
    for nombre_hoja in ("LIQUIDACION", "MO_MANUAL", "MANO_OBRA", "SIP_MANO_OBRA"):
        if nombre_hoja not in wb.sheetnames:
            continue
        resultado = _distribucion_de_hoja(wb[nombre_hoja])
        if resultado:
            return resultado
    return []


def _distribucion_de_hoja(ws) -> list:
    resultado = []
    en_dist = False
    for r in range(1, ws.max_row + 1):
        v1 = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
        if not en_dist:
            if ("DISTRIB" in v1 and "LIQUID" in v1) or \
               ("LIDERES" in v1 and "LIQUID" in v1):
                en_dist = True
            continue

        # Saltar la fila de encabezado de tabla (#, Cédula, Nombre...)
        v1_raw = str(ws.cell(r, 1).value or "").strip()
        try:
            num = int(float(v1_raw))
            if num < 1:
                continue
        except (ValueError, TypeError):
            continue   # fila de header o blanca → saltar

        cedula = str(ws.cell(r, 2).value or "").strip()
        nombre = str(ws.cell(r, 3).value or "").strip()
        pct_raw = ws.cell(r, 4).value

        # La cédula puede faltar en la hoja (se resuelve por nombre contra SGS
        # en la verificación); descartar la fila perdería su porcentaje.
        if not nombre:
            continue

        # Normalizar cédula → formato C\d+
        ced_up = cedula.upper()
        if ced_up.startswith("C") and ced_up[1:].isdigit():
            cedula = ced_up
        elif cedula.isdigit():
            cedula = "C" + cedula
        else:
            cedula = ced_up

        # Parsear porcentaje: puede llegar como "25%", 0.25 (decimal) o 25 (entero)
        if isinstance(pct_raw, (int, float)):
            pct = float(pct_raw)
            if 0 < pct <= 1.0:
                pct = pct * 100.0   # decimal → %
        else:
            pct_str = str(pct_raw or "").strip().replace("%", "").replace(",", ".")
            try:
                pct = float(pct_str)
                if 0 < pct <= 1.0:
                    pct = pct * 100.0
            except (ValueError, TypeError):
                continue

        resultado.append({"cedula": cedula, "nombre": nombre, "pct": round(pct, 2)})

    return resultado


def extraer_fecha_ejecucion(wb: openpyxl.Workbook) -> str:
    """
    Lee 'Fecha real de ejecucion:' de la hoja LIQUIDACION (formato nuevo).
    Devuelve 'YYYY-MM-DD' o '' si la hoja/fila no existe.

    SOLO lee la hoja LIQUIDACION: el 'Fecha ejec.:' de SIP_MANO_OBRA es la
    fecha programada del SIP (puede diferir semanas de la real) — usarla como
    default escribiría fechas equivocadas en SGS.
    """
    if "LIQUIDACION" not in wb.sheetnames:
        return ""
    ws = wb["LIQUIDACION"]
    for r in range(1, min(ws.max_row, 30) + 1):
        rotulo = _sin_tildes(str(ws.cell(r, 1).value or "").upper())
        if "FECHA" not in rotulo:
            continue
        if not any(k in rotulo for k in ("EJEC", "REAL")):
            continue
        val = ws.cell(r, 2).value
        if val is None or str(val).strip() == "":
            continue
        if isinstance(val, datetime.datetime):
            return val.strftime("%Y-%m-%d")
        if isinstance(val, datetime.date):
            return val.strftime("%Y-%m-%d")
        txt = str(val).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
            try:
                return datetime.datetime.strptime(txt[:10], fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""


def parsear_mat(wb: openpyxl.Workbook) -> tuple[list, list]:
    """
    Extrae materiales. Prioridad: hojas N<nodo> (v2) > SGS_BALANCE (v1) > CRUCE_LIQUIDADOR/CRUCE_MAT.
    Cada row: {fila, nodo, codigo, descripcion, sip_cant, sgs_neto_excel, estado_excel}
    """
    rows, errs = [], []

    # v2: hojas N<nodo> individuales (balance_sgs_manual nuevo formato)
    nodo_sheet_names = [s for s in wb.sheetnames if re.match(r"^N\d+[A-Z]?(?:_N\d+[A-Z]?)*$", s)]
    if nodo_sheet_names:
        rows = _parsear_mat_nodo_sheets(wb, errs)
        if rows:
            errs.append(
                f"[INFO] Fuente MAT: hojas N<nodo> "
                f"({len(nodo_sheet_names)} nodos, {len(rows)} filas, códigos SAP)."
            )
            return rows, errs

    # v1: hoja única SGS_BALANCE
    if "SGS_BALANCE" in wb.sheetnames:
        rows = _parsear_mat_sgsbalance(wb["SGS_BALANCE"], errs)
        if rows:
            errs.append(f"[INFO] Fuente MAT: SGS_BALANCE ({len(rows)} filas, códigos SAP).")
            return rows, errs

    hoja = next((h for h in HOJAS_CRUCE_MAT if h in wb.sheetnames), None)
    if hoja is None:
        errs.append("Hoja de materiales no encontrada ('N<nodo>', 'SGS_BALANCE', 'CRUCE_LIQUIDADOR' o 'CRUCE_MAT').")
        return rows, errs

    ws = wb[hoja]
    col = {"nodo": None, "cod": None, "desc": None, "sip": None, "sgs": None, "estado": None}
    header_row = None

    for r in range(1, min(ws.max_row + 1, 15)):
        vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 20)]
        tmp = {"nodo": None, "cod": None, "desc": None,
               "sip": None, "sgs": None, "estado": None}
        for i, v in enumerate(vals):
            if v == "NODO"                   and tmp["nodo"]   is None: tmp["nodo"]   = i + 1
            if ("CODIGO" in v or v == "COD") and tmp["cod"]    is None: tmp["cod"]    = i + 1
            if "DESCRIPCION" in v            and tmp["desc"]   is None: tmp["desc"]   = i + 1
            if "SIP" in v and "CANT" in v    and tmp["sip"]    is None: tmp["sip"]    = i + 1
            if "SGS" in v and "NETO" in v    and tmp["sgs"]    is None: tmp["sgs"]    = i + 1
            if v == "ESTADO"                 and tmp["estado"] is None: tmp["estado"] = i + 1
        if tmp["cod"] and tmp["sgs"]:
            col        = tmp
            header_row = r
            break

    if not header_row:
        errs.append("Cabecera no detectada (busca 'Codigo' y 'SGS Neto').")
        return rows, errs

    current_nodo     = "1"
    current_pro_code = ""
    current_pdl      = ""
    for r in range(header_row + 1, ws.max_row + 1):
        fila_vals = [str(ws.cell(r, c).value or "").strip() for c in range(1, 5)]
        fila_str  = " ".join(fila_vals)
        fila_norm = _sin_tildes(fila_str.upper())

        if "NODO:" in fila_norm:
            # Captura el rango completo (AL23 / _N23 / -N23), no solo el primer
            # numero — mismo riesgo de truncar nodos cable que en SGS_BALANCE.
            m = re.search(r"\d+(?:\s*AL\s*\d+|[_\-]N?\d+)?", fila_str)
            pro_m = re.search(r'\bPRO:\s*(PRO-\d+)', fila_norm)
            current_pro_code = pro_m.group(1) if pro_m else ""
            # PDL del bloque — plantilla MAT masiva multi-proyecto:
            # "NODO: n | PRO: PRO-x | PDL: CDOPP...;CDOPP..." / "PDL: DF2026...".
            # No hay PDL global en esa plantilla; se conserva por fila.
            pdl_m = re.search(r'\bPDL:\s*([^|]+)', fila_norm)
            current_pdl = _normalizar_pdl(pdl_m.group(1)) if pdl_m else ""
            if m:
                current_nodo = m.group()
            else:
                nodo_ia = _minimax_extraer_nodo(fila_str)
                if nodo_ia:
                    errs.append(f"[INFO] Nodo interpretado por IA: '{fila_str}' -> '{nodo_ia}'")
                    current_nodo = nodo_ia
                else:
                    errs.append(f"[WARN] No se reconoció el nodo en: '{fila_str}' — sección omitida.")
                    current_nodo = ""
            continue
        if any(k in fila_norm for k in ("RESUMEN", "VERDE", "ROJO", "AMBAR", "GRIS")):
            continue

        cod = str(ws.cell(r, col["cod"]).value or "").strip()
        if not cod or not any(ch.isdigit() for ch in cod):
            continue

        nodo_v = str(ws.cell(r, col["nodo"]).value or "").strip() if col["nodo"] else current_nodo
        desc   = str(ws.cell(r, col["desc"]).value or "").strip() if col["desc"] else ""
        estado = str(ws.cell(r, col["estado"]).value or "").strip().upper() if col["estado"] else ""

        sip_raw  = ws.cell(r, col["sip"]).value if col["sip"] else None
        sip_cant = None
        if sip_raw is not None and str(sip_raw).strip() not in ("", "-", "?", "—", "—", "–"):
            sip_cant = _parse_float(sip_raw)

        # CRUCE_LIQUIDADOR: solo filas en AMBOS SGS y SIP.
        # CRUCE_MAT: no tiene columna SIP → aceptar todas las filas con cod válido.
        if col["sip"] and sip_cant is None:
            continue

        sgs_raw  = ws.cell(r, col["sgs"]).value if col["sgs"] else None
        sgs_neto = _parse_float(sgs_raw) or 0.0

        rows.append({
            "fila":           r,
            "nodo":           nodo_v or current_nodo,
            "pro_code":       current_pro_code,
            "pdl":            current_pdl,
            "codigo":         cod,
            "descripcion":    desc,
            "sip_cant":       sip_cant,
            "sgs_neto_excel": sgs_neto,
            "estado_excel":   estado,
        })

    return rows, errs


def es_plantilla_mat_masiva(wb: openpyxl.Workbook) -> bool:
    """
    True si el libro es la 'Plantilla masiva de MATERIALES' que genera el
    Verificador SGS (pestaña 'Automático por Mes').

    Solo materiales, varios proyectos en un mismo archivo (PRO explícito por
    bloque en CRUCE_LIQUIDADOR), sin mano de obra, sin fecha y sin PDL global.
    Se detecta por el título de la fila 1 y, como respaldo, por la pareja de
    hojas CRUCE_LIQUIDADOR + DETALLE_MATERIALES que solo trae esa plantilla.
    """
    for ws in wb.worksheets:
        for c in range(1, 4):
            v = _sin_tildes(str(ws.cell(1, c).value or "").upper())
            if "PLANTILLA MASIVA DE MATERIALES" in v:
                return True
    return any(h in wb.sheetnames for h in HOJAS_CRUCE) and "DETALLE_MATERIALES" in wb.sheetnames
