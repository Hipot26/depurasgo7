"""
parser_presupuesto.py — Extrae MO y MAT del Excel de Presupuesto (módulo julian).
Formato: hoja "Presupuesto" con secciones MANO DE OBRA y MATERIALES.
Solo lectura de archivos locales. No toca SGS.
"""

import re
import unicodedata
import openpyxl


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sin_tildes(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", str(s))
        if unicodedata.category(c) != "Mn"
    )


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


def _extraer_lcl(texto: str) -> str:
    """Extrae código LCL... o DF... de una cadena como 'CLIENTES 360_CTO_NEUSA LCL6300985782'."""
    if not texto:
        return ""
    texto_up = str(texto).upper()
    # LCL/6300 de programados. En SGS el order_number queda como 6300...
    m = re.search(r"LCL\s*[-_:]?\s*(6300\d+)", texto_up)
    if m:
        return m.group(1)
    m = re.search(r"\b(6300\d{4,})\b", texto_up)
    if m:
        return m.group(1)
    # DF seguido de dígitos/letras (formato alternativo)
    m = re.search(r"(DF[\w\d]+)", texto_up)
    if m:
        return m.group(1)
    return ""


# ── Metadatos de cabecera ─────────────────────────────────────────────────────

def _leer_metadatos(ws) -> dict:
    """
    Lee los metadatos del encabezado del Presupuesto (filas 9-14).
    Retorna dict con: proyecto, punto_electrico, proceso, municipio, lcl.
    """
    meta = {
        "proyecto":        "",
        "punto_electrico": "",
        "proceso":         "",
        "municipio":       "",
        "lcl":             "",
    }

    for r in range(1, 20):
        vals = [str(ws.cell(r, c).value or "").strip() for c in range(1, 17)]
        fila_norm = _sin_tildes(" ".join(vals).upper())

        # Fila con etiquetas "PROYECTO" / "PUNTO ELECTRICO"
        if "PROYECTO" in fila_norm and "EMPRESA" not in fila_norm:
            # Los valores suelen estar en la fila siguiente
            vals_sig = [str(ws.cell(r + 1, c).value or "").strip() for c in range(1, 17)]
            for i, v in enumerate(vals_sig):
                v_norm = _sin_tildes(v.upper())
                if not meta["proceso"] and "MEDIDA" in v_norm:
                    meta["proceso"] = v.strip()
                if not meta["proyecto"] and re.search(r"LCL\d+", v.upper()):
                    meta["proyecto"] = v.strip()
                    meta["lcl"] = _extraer_lcl(v)
                if not meta["punto_electrico"] and re.match(r"S\d{4,}", v.upper()):
                    meta["punto_electrico"] = v.strip()

        # Fila con "MUNICIPIO"
        if "MUNICIPIO" in fila_norm and "LOCALIDAD" in fila_norm:
            vals_sig = [str(ws.cell(r + 1, c).value or "").strip() for c in range(1, 5)]
            if vals_sig[0]:
                meta["municipio"] = vals_sig[0]

    return meta


# ── Parser MO ─────────────────────────────────────────────────────────────────

def _parsear_datos_ejecutados(ws) -> tuple[list, list]:
    """
    Parsea el formato "DATOS EJECUTADOS":
    Columnas: DF | NODO | PRESTACION | CANTIDAD | LIDER | FECHA EJECION | MO | PDL ASOCIADO
    Múltiples DFs por archivo. Solo MO (códigos QP).
    Retorna (rows_mo, errs). Lista vacía si la hoja no coincide con este formato.
    """
    rows, errs = [], []

    col = {"df": None, "nodo": None, "prest": None, "qty": None, "lider": None}
    header_row = None

    for r in range(1, min(15, ws.max_row + 1)):
        vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 12)]
        has_df    = any(v == "DF" for v in vals)
        has_prest = any("PRESTACION" in v for v in vals)
        has_qty   = any(v in ("CANTIDAD", "CANT") for v in vals)
        if has_df and has_prest and has_qty:
            for i, v in enumerate(vals):
                if v == "DF"                         and col["df"]    is None: col["df"]    = i + 1
                if v == "NODO"                        and col["nodo"]  is None: col["nodo"]  = i + 1
                if "PRESTACION" in v                  and col["prest"] is None: col["prest"] = i + 1
                if v in ("CANTIDAD", "CANT")           and col["qty"]   is None: col["qty"]   = i + 1
                if "LIDER" in v                        and col["lider"] is None: col["lider"] = i + 1
            if col["df"] and col["prest"] and col["qty"]:
                header_row = r
                break

    if not header_row:
        return rows, errs  # no es este formato

    errs.append(f"[INFO] Formato DATOS EJECUTADOS detectado en hoja '{ws.title}'")

    current_df = ""
    vistos = set()

    for r in range(header_row + 1, ws.max_row + 1):
        df_raw    = ws.cell(r, col["df"]).value    if col["df"]    else None
        prest_raw = ws.cell(r, col["prest"]).value if col["prest"] else None
        qty_raw   = ws.cell(r, col["qty"]).value   if col["qty"]   else None

        # DF puede venir en cada fila o solo en la primera del grupo
        if df_raw and str(df_raw).strip():
            current_df = str(df_raw).strip().upper()

        prest = str(prest_raw or "").strip().upper()
        if not prest or not any(c.isalpha() for c in prest):
            continue

        qty = _parse_float(qty_raw)
        if qty is None or qty == 0:
            continue

        if not current_df:
            continue

        nodo_raw = ws.cell(r, col["nodo"]).value if col["nodo"] else None
        nodo = str(int(nodo_raw)) if isinstance(nodo_raw, (int, float)) else str(nodo_raw or "1").strip()
        if not nodo:
            nodo = "1"

        lider = str(ws.cell(r, col["lider"]).value or "").strip() if col["lider"] else ""

        clave = (current_df, nodo, prest)
        if clave in vistos:
            errs.append(f"Fila {r}: duplicado ({current_df}, nodo={nodo}, {prest}) — acumulando")
            for ex in rows:
                if ex["_odm"] == current_df and ex["pro"] == nodo and ex["codigo"] == prest:
                    ex["cantidad"] += qty
                    break
            continue
        vistos.add(clave)

        rows.append({
            "fila":           r,
            "pro":            nodo,       # NODO numérico → verificador busca project.dominion
            "codigo":         prest,      # QP code → código en SGS
            "cantidad":       qty,
            "lider":          lider,
            "qp_code":        prest,
            "baremo":         "",
            "descripcion":    "",
            "tipo_actividad": "",
            "_odm":           current_df, # DF code → identificador de proyecto en SGS
        })

    if not rows:
        errs.append("[WARN] Formato DATOS EJECUTADOS: sin filas MO válidas.")

    return rows, errs


def parsear_mo_presupuesto(wb: openpyxl.Workbook) -> tuple[list, list]:
    """
    Extrae filas de Mano de Obra.
    Soporta tres formatos:
      · Hoja 'Presupuesto' con sección MANO DE OBRA (formato original)
      · Hoja 'RG11_budget' con tabla plana MO+MAT mezclados
      · Tabla plana multi-DF: DF | NODO | PRESTACION | CANTIDAD | LIDER (DATOS EJECUTADOS)
    """
    rows, errs = [], []

    # ── Detección de formato RG11_budget ─────────────────────────────────────
    for nombre in ("RG11_budget", "rg11_budget"):
        if nombre in wb.sheetnames:
            try:
                ws_rg = wb[nombre]
                rows_mo, errs_mo, _, _, _ = _parsear_rg11_budget(ws_rg)
                return rows_mo, errs_mo
            except Exception as ex:
                errs.append(f"Error parseando RG11_budget MO: {ex}")
                break

    # ── Detección de formato DATOS EJECUTADOS (multi-DF, tabla plana) ─────────
    for ws_name in wb.sheetnames:
        try:
            rows_ej, errs_ej = _parsear_datos_ejecutados(wb[ws_name])
            if rows_ej:
                return rows_ej, errs_ej
        except Exception:
            pass

    # ── Formato original: hoja Presupuesto ───────────────────────────────────
    ws = None
    for nombre in ("Presupuesto", "PRESUPUESTO", "BASE PRES"):
        if nombre in wb.sheetnames:
            try:
                _ = wb[nombre].dimensions  # falla en chartsheets
                ws = wb[nombre]
                break
            except AttributeError:
                continue
    if ws is None:
        errs.append("No se encontró hoja 'Presupuesto' en el archivo.")
        return rows, errs

    # Leer metadatos
    meta = _leer_metadatos(ws)
    if meta["lcl"]:
        errs.append(f"[INFO] Proyecto: {meta['proyecto']} — LCL: {meta['lcl']}")

    # Encontrar fila de cabecera MO (tiene "CODIGO E4E" + "ACTIVIDAD" + "CANT")
    # CODIGO E4E = QP1098 → es el código que va a SGS (campo "codigo")
    # ACTIVIDAD  = AHO110 → es el baremo/descripcion técnica (campo "baremo")
    col = {"nodo": None, "qp": None, "baremo": None, "cant": None, "lider": None,
           "solicitud": None, "tipo": None, "desc": None}
    header_row = None
    mo_section = False

    for r in range(1, ws.max_row + 1):
        vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 18)]
        fila_str = " ".join(vals)

        # Detectar inicio de sección MO
        if "PRESUPUESTO MANO DE OBRA" in fila_str:
            mo_section = True
            continue

        # Detectar fin de sección MO (inicio de MATERIALES)
        if mo_section and "PRESUPUESTO MATERIALES" in fila_str:
            break

        # Detectar fila de cabecera
        if mo_section and "CODIGO E4E" in fila_str and "CANT" in fila_str:
            for i, v in enumerate(vals):
                if "NODO" == v and col["nodo"] is None:
                    col["nodo"] = i + 1
                if "CODIGO E4E" in v and col["qp"] is None:
                    col["qp"] = i + 1
                if v == "ACTIVIDAD" and col["baremo"] is None:
                    col["baremo"] = i + 1
                if v in ("CANT", "CANTIDAD") and col["cant"] is None:
                    col["cant"] = i + 1
                if "GRUPO TECNICO" in v and col["lider"] is None:
                    col["lider"] = i + 1
                if "SOLICITUD" in v and col["solicitud"] is None:
                    col["solicitud"] = i + 1
                if "TIPO ACTIVIDAD" in v and col["tipo"] is None:
                    col["tipo"] = i + 1
                if "DESCRIPCI" in v and col["desc"] is None:
                    col["desc"] = i + 1
            if col["qp"] and col["cant"]:
                header_row = r
                break

    # Fallback: si no tiene sección explícita, buscar cabecera directamente
    if not header_row:
        for r in range(1, min(ws.max_row + 1, 40)):
            vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 18)]
            fila_str = " ".join(vals)
            if "CODIGO E4E" in fila_str and "CANT" in fila_str:
                for i, v in enumerate(vals):
                    if "NODO" == v and col["nodo"] is None: col["nodo"] = i + 1
                    if "CODIGO E4E" in v and col["qp"] is None: col["qp"] = i + 1
                    if v == "ACTIVIDAD" and col["baremo"] is None: col["baremo"] = i + 1
                    if v in ("CANT", "CANTIDAD") and col["cant"] is None: col["cant"] = i + 1
                    if "GRUPO TECNICO" in v and col["lider"] is None: col["lider"] = i + 1
                    if "SOLICITUD" in v and col["solicitud"] is None: col["solicitud"] = i + 1
                    if "TIPO ACTIVIDAD" in v and col["tipo"] is None: col["tipo"] = i + 1
                    if "DESCRIPCI" in v and col["desc"] is None: col["desc"] = i + 1
                if col["qp"] and col["cant"]:
                    header_row = r
                    break

    if not header_row:
        errs.append("No se detectó cabecera MO (busca 'CODIGO E4E' + 'ACTIVIDAD' + 'CANT').")
        return rows, errs

    # LCL derivado de columna SOLICITUD si no se pudo del metadato
    lcl_global = meta.get("lcl", "")

    vistos = set()
    for r in range(header_row + 1, ws.max_row + 1):
        # Detectar fin de sección
        fila_c1 = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
        if "TOTAL" in fila_c1 or "GRAN TOTAL" in fila_c1:
            break
        if "PRESUPUESTO MATERIALES" in fila_c1:
            break

        qp_raw     = ws.cell(r, col["qp"]).value     if col["qp"]     else None
        baremo_raw = ws.cell(r, col["baremo"]).value if col["baremo"] else None
        cant_raw   = ws.cell(r, col["cant"]).value   if col["cant"]   else None

        # CODIGO E4E = QP1098 → este es el código que va a SGS
        qp     = str(qp_raw     or "").strip().upper()
        baremo = str(baremo_raw or "").strip().upper()  # AHO110 — info adicional

        # Necesitamos al menos el QP code
        if not qp:
            continue
        # Saltar filas de subtotal / separador (el QP siempre empieza con letras)
        if not any(c.isalpha() for c in qp):
            continue

        qty = _parse_float(cant_raw)
        if qty is None or qty == 0:
            continue

        nodo_raw = ws.cell(r, col["nodo"]).value if col["nodo"] else None
        nodo = str(int(nodo_raw)) if isinstance(nodo_raw, float) else str(nodo_raw or "").strip()

        # LCL de la columna SOLICITUD (más preciso que el metadato)
        if col["solicitud"]:
            sol = str(ws.cell(r, col["solicitud"]).value or "")
            lcl_fila = _extraer_lcl(sol) or lcl_global
        else:
            lcl_fila = lcl_global

        lider = str(ws.cell(r, col["lider"]).value or "").strip() if col["lider"] else ""
        tipo  = str(ws.cell(r, col["tipo"]).value  or "").strip() if col["tipo"]  else ""
        desc  = str(ws.cell(r, col["desc"]).value  or "").strip() if col["desc"]  else ""

        clave = (nodo, qp)
        if clave in vistos:
            errs.append(f"Fila {r}: duplicado (nodo={nodo}, qp={qp}) — acumulando cantidad")
            for existing in rows:
                if (existing["pro"], existing["codigo"]) == (nodo, qp):
                    existing["cantidad"] += qty
                    break
            continue
        vistos.add(clave)

        rows.append({
            "fila":           r,
            "pro":            nodo,
            "codigo":         qp,       # ← QP1098 es el código SGS
            "cantidad":       qty,
            "lider":          lider,
            "qp_code":        qp,       # también guardado aparte para display
            "baremo":         baremo,   # AHO110 — solo informativo
            "descripcion":    desc,
            "tipo_actividad": tipo,
            "_odm":           lcl_fila,
        })

    if not rows:
        errs.append("No se extrajeron filas MO. Verifica que la hoja 'Presupuesto' tenga datos.")

    return rows, errs


def extraer_pdl_presupuesto(wb: openpyxl.Workbook) -> str:
    """
    Extrae el valor de PDL / Ticket del encabezado del Presupuesto.
    Busca etiquetas 'TICKET' o 'PDL' en las primeras filas de la hoja Presupuesto.
    Retorna el valor encontrado o cadena vacía.
    """
    for nombre in ("Presupuesto", "PRESUPUESTO", "BASE PRES"):
        if nombre not in wb.sheetnames:
            continue
        try:
            ws = wb[nombre]
        except Exception:
            continue
        for r in range(1, min(ws.max_row + 1, 30)):
            for col in range(1, 6):
                label = _sin_tildes(str(ws.cell(r, col).value or "").upper().strip())
                if "TICKET" in label or label == "PDL":
                    # Valor esperado en la celda a la derecha
                    for offset in (1, 2):
                        v = str(ws.cell(r, col + offset).value or "").strip()
                        if v and v.upper() not in ("", "N/A", "-"):
                            return v
    return ""


def extraer_total_mo_presupuesto(wb: openpyxl.Workbook) -> float | None:
    """Lee la fila TOTAL de la sección MO en hoja Presupuesto."""
    for nombre in ("Presupuesto", "PRESUPUESTO"):
        if nombre not in wb.sheetnames:
            continue
        try:
            ws = wb[nombre]
        except Exception:
            continue
        for r in range(1, ws.max_row + 1):
            label = _sin_tildes(str(ws.cell(r, 13).value or "").upper().strip())
            if label in ("TOTAL", "GRAN TOTAL"):
                val = _parse_float(ws.cell(r, 14).value)
                if val and val > 0:
                    return val
    return None


# ── Parser formato RG11_budget (tabla plana con MO y MAT mezclados) ──────────

def _extraer_lcl_encabezado(ws) -> str:
    """Lee las primeras 5 filas buscando un valor con LCL/6300 en cualquier celda."""
    for r in range(1, 6):
        for c in range(1, 20):
            val = str(ws.cell(r, c).value or "").strip()
            if not val:
                continue
            lcl = _extraer_lcl(val)
            if lcl:
                return lcl
    return ""


def _es_codigo_mo(codigo: str) -> bool:
    """True si el código tiene letras (QP1176, AHO717…) → MO. False si es puro numérico → MAT."""
    return bool(codigo) and any(c.isalpha() for c in codigo)


def _parsear_rg11_budget(ws) -> tuple[list, list, list, list, str]:
    """
    Parsea la hoja RG11_budget (tabla plana: MO y MAT mezclados en las mismas filas).

    Estructura esperada:
      Filas 1-4: metadatos (LCL en alguna celda)
      Fila 5+:   cabecera con columnas nodo | tipo_nodo | tipo_trabajo | nivel_tension |
                 cod_prest/mat | quantity | descripcion | … | BUSQUEDA_CODIGO | CODIGO_NUEVO | CANT | ACTIVIDAD_MATERIAL
      Rows 6+:   datos
        · col 5 con letras → fila MO (QP1176)
        · col 5 numérico   → fila MAT (231052)

    Retorna (rows_mo, errs_mo, rows_mat, errs_mat, lcl)
    """
    rows_mo, rows_mat, errs_mo, errs_mat = [], [], [], []

    lcl = _extraer_lcl_encabezado(ws)

    # Buscar fila de cabecera (contiene "nodo" en col 1 y "quantity" o "cant" en fila)
    header_row = None
    col_nodo = col_codigo = col_qty = col_baremo = col_cant2 = col_desc2 = None
    for r in range(1, min(15, ws.max_row + 1)):
        vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 16)]
        fila_str = " ".join(vals)
        if "NODO" in fila_str and ("QUANTITY" in fila_str or "COD" in fila_str):
            for i, v in enumerate(vals):
                if v == "NODO" and col_nodo is None:
                    col_nodo = i + 1
                if ("COD_PREST" in v or "COD PREST" in v or "CODIGO NUEVO" in v) and col_codigo is None:
                    col_codigo = i + 1
                if v in ("QUANTITY", "CANT", "CANTIDAD") and col_qty is None:
                    col_qty = i + 1
                if "BUSQUEDA" in v and "CODIGO" in v and col_baremo is None:
                    col_baremo = i + 1
                if "ACTIVIDAD" in v and col_desc2 is None:
                    col_desc2 = i + 1
            # Segundo CANT (col 14 en el formato observado)
            for i, v in enumerate(vals):
                if v == "CANT" and col_cant2 is None and (i + 1) != col_qty:
                    col_cant2 = i + 1
            if col_nodo and col_qty:
                header_row = r
                break

    if not header_row:
        errs_mo.append("RG11_budget: no se detectó cabecera (busca columna 'nodo' + 'quantity').")
        return rows_mo, errs_mo, rows_mat, errs_mat, lcl

    # Valores por defecto según posición observada si la detección falló
    if col_nodo   is None: col_nodo   = 1
    if col_codigo is None: col_codigo = 5
    if col_qty    is None: col_qty    = 6
    if col_baremo is None: col_baremo = 12

    vistos_mo, vistos_mat = set(), set()

    for r in range(header_row + 1, ws.max_row + 1):
        nodo_raw = ws.cell(r, col_nodo).value
        cod_raw  = ws.cell(r, col_codigo).value

        if nodo_raw is None and cod_raw is None:
            continue

        nodo   = str(int(nodo_raw)) if isinstance(nodo_raw, (int, float)) else str(nodo_raw or "").strip()
        codigo = str(int(cod_raw))  if isinstance(cod_raw,  (int, float)) else str(cod_raw  or "").strip()

        if not nodo or not codigo:
            continue

        # Cantidad: columna principal; fallback a segunda columna CANT
        qty = _parse_float(ws.cell(r, col_qty).value)
        if (qty is None or qty == 0) and col_cant2:
            qty = _parse_float(ws.cell(r, col_cant2).value)
        if qty is None or qty == 0:
            continue

        # Descripción: preferir columna ACTIVIDAD O MATERIAL (larga), fallback col 7
        desc = ""
        if col_desc2:
            desc = str(ws.cell(r, col_desc2).value or "").strip()
        if not desc:
            desc = str(ws.cell(r, 7).value or "").strip()

        if _es_codigo_mo(codigo):
            # ── Fila MO ──────────────────────────────────────────────────────
            baremo = str(ws.cell(r, col_baremo).value or "").strip() if col_baremo else ""
            clave = (nodo, codigo)
            if clave in vistos_mo:
                for ex in rows_mo:
                    if (ex["pro"], ex["codigo"]) == (nodo, codigo):
                        ex["cantidad"] += qty
                        break
                continue
            vistos_mo.add(clave)
            rows_mo.append({
                "fila": r, "pro": nodo, "codigo": codigo,
                "cantidad": qty, "lider": "", "qp_code": codigo,
                "baremo": baremo, "descripcion": desc,
                "tipo_actividad": "", "_odm": lcl,
            })
        else:
            # ── Fila MAT ─────────────────────────────────────────────────────
            clave = (nodo, codigo)
            if clave in vistos_mat:
                for ex in rows_mat:
                    if (ex["nodo"], ex["codigo"]) == (nodo, codigo):
                        ex["sgs_neto_excel"] = (ex["sgs_neto_excel"] or 0) + qty
                        break
                continue
            vistos_mat.add(clave)
            rows_mat.append({
                "fila": r, "nodo": nodo, "codigo": codigo,
                "descripcion": desc, "sip_cant": None,
                "sgs_neto_excel": qty, "estado_excel": "",
                "tipo_material": "", "_odm": lcl,
            })

    if not rows_mo:
        errs_mo.append("[INFO] RG11_budget: sin filas MO válidas.")
    if not rows_mat:
        errs_mat.append("[INFO] RG11_budget: sin filas MAT válidas.")

    return rows_mo, errs_mo, rows_mat, errs_mat, lcl


# ── Parser MAT ────────────────────────────────────────────────────────────────

def parsear_mat_presupuesto(wb: openpyxl.Workbook) -> tuple[list, list]:
    """
    Extrae materiales.
    Soporta dos formatos:
      · Hoja 'Presupuesto' con sección PRESUPUESTO MATERIALES (formato original)
      · Hoja 'RG11_budget' con tabla plana MO+MAT mezclados (formato nuevo)
    """
    rows, errs = [], []

    # ── Detección de formato RG11_budget ─────────────────────────────────────
    for nombre in ("RG11_budget", "rg11_budget"):
        if nombre in wb.sheetnames:
            try:
                ws_rg = wb[nombre]
                _, _, rows_mat, errs_mat, _ = _parsear_rg11_budget(ws_rg)
                return rows_mat, errs_mat
            except Exception as ex:
                errs.append(f"Error parseando RG11_budget MAT: {ex}")
                break

    # ── Formato original: hoja Presupuesto ───────────────────────────────────
    ws = None
    for nombre in ("Presupuesto", "PRESUPUESTO"):
        if nombre in wb.sheetnames:
            try:
                _ = wb[nombre].dimensions
                ws = wb[nombre]
                break
            except AttributeError:
                continue
    if ws is None:
        errs.append("Hoja 'Presupuesto' no encontrada para MAT.")
        return rows, errs

    # Buscar sección PRESUPUESTO MATERIALES
    mat_start = None
    for r in range(1, ws.max_row + 1):
        fila_str = _sin_tildes(str(ws.cell(r, 1).value or "").upper().strip())
        if "PRESUPUESTO MATERIALES" in fila_str:
            mat_start = r
            break

    if not mat_start:
        errs.append("[INFO] No se encontró sección 'PRESUPUESTO MATERIALES' — sin materiales.")
        return rows, errs

    # Buscar cabecera MAT
    # Columnas de interés:
    #   CODIGO SAP NUEVO → código que usa SGS en project.material (ej. 230965)
    #   BALANCE          → CANT - REINTEGRO = cantidad neta a liquidar  ← usar esto
    #   CANT             → total despachado (no usar como cantidad)
    col = {"nodo": None, "sap_nuevo": None, "sap_viejo": None, "tipo_mat": None,
           "desc": None, "cant": None, "reintegro": None, "balance": None,
           "solicitud": None}
    header_row = None

    for r in range(mat_start, min(mat_start + 10, ws.max_row + 1)):
        vals = [_sin_tildes(str(ws.cell(r, c).value or "").upper().strip()) for c in range(1, 18)]
        fila_str = " ".join(vals)
        if "NODO" in fila_str and "CANT" in fila_str:
            for i, v in enumerate(vals):
                if v == "NODO"                                          and col["nodo"]      is None: col["nodo"]      = i + 1
                if "CODIGO SAP NUEVO" in v                              and col["sap_nuevo"] is None: col["sap_nuevo"] = i + 1
                if "CODIGO SAP VIEJO" in v                              and col["sap_viejo"] is None: col["sap_viejo"] = i + 1
                if ("TIPO DE MATERIAL" in v or v == "TIPO MAT")        and col["tipo_mat"]  is None: col["tipo_mat"]  = i + 1
                if "DESCRIPCI" in v                                     and col["desc"]      is None: col["desc"]      = i + 1
                if v in ("CANT", "CANTIDAD")                            and col["cant"]      is None: col["cant"]      = i + 1
                if v == "REINTEGRO"                                     and col["reintegro"] is None: col["reintegro"] = i + 1
                if v == "BALANCE"                                       and col["balance"]   is None: col["balance"]   = i + 1
                if "SOLICITUD" in v                                     and col["solicitud"] is None: col["solicitud"] = i + 1
            if col["nodo"] and col["sap_nuevo"]:
                header_row = r
                break

    if not header_row:
        errs.append("No se detectó cabecera MAT en sección PRESUPUESTO MATERIALES.")
        return rows, errs

    lcl_global = ""
    for r in range(header_row + 1, ws.max_row + 1):
        nodo_raw = ws.cell(r, col["nodo"]).value     if col["nodo"]     else None
        sap_raw  = ws.cell(r, col["sap_nuevo"]).value if col["sap_nuevo"] else None

        # Si las celdas clave están vacías, saltar
        if nodo_raw is None and sap_raw is None:
            continue

        nodo = str(int(nodo_raw)) if isinstance(nodo_raw, (int, float)) else str(nodo_raw or "").strip()

        # Código que usa SGS = CODIGO SAP NUEVO (ej. 230965)
        codigo = str(int(sap_raw)) if isinstance(sap_raw, (int, float)) else str(sap_raw or "").strip()

        if not nodo or not codigo:
            continue

        # Cantidad = BALANCE (CANT - REINTEGRO) → lo que realmente queda pendiente
        # Si BALANCE no existe como columna, calcularlo: CANT - REINTEGRO
        balance_raw   = ws.cell(r, col["balance"]).value   if col["balance"]   else None
        cant_raw      = ws.cell(r, col["cant"]).value      if col["cant"]      else None
        reintegro_raw = ws.cell(r, col["reintegro"]).value if col["reintegro"] else None

        cant = _parse_float(balance_raw)
        if cant is None:
            # Fallback: calcular CANT - REINTEGRO
            c_val = _parse_float(cant_raw)      or 0.0
            r_val = _parse_float(reintegro_raw) or 0.0
            cant  = round(c_val - r_val, 6)

        if cant is None:
            continue

        desc     = str(ws.cell(r, col["desc"]).value     or "").strip() if col["desc"]     else ""
        tipo_mat = str(ws.cell(r, col["tipo_mat"]).value or "").strip() if col["tipo_mat"] else ""

        if col["solicitud"]:
            sol = str(ws.cell(r, col["solicitud"]).value or "")
            lcl = _extraer_lcl(sol) or lcl_global
            if lcl:
                lcl_global = lcl
        else:
            lcl = lcl_global

        rows.append({
            "fila":           r,
            "nodo":           nodo,
            "codigo":         codigo,
            "descripcion":    desc,
            "sip_cant":       None,        # no hay SIP en este formato
            "sgs_neto_excel": cant,
            "estado_excel":   tipo_mat,    # PROPIO / APORTACION
            "tipo_material":  tipo_mat,
            "_odm":           lcl,
        })

    if not rows:
        errs.append("[INFO] Sección MAT encontrada pero sin filas válidas.")

    return rows, errs
