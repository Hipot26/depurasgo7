"""
leer_reporte_produccion.py — Lee el Informe de Producción SGS (Excel)
======================================================================
Fuente de datos: "Reporte → Informe de Producción" descargado desde el UI de SGS.
Filtros aplicados:
  - Región         = UBATE  (o variantes: UBATÉ, ubate…)
  - Projecto/Tecnología IN [MAYOR IMPACTO, AMP ELECTRICO]
  - Fecha Avance   en el mes/año objetivo

Columnas esperadas en el Excel (pueden venir en cualquier orden):
  Región, Projecto, Producción total, Projecto/Tecnología, Fecha Avance,
  Ejecutor, Ejecutor/NúmeroIdentificación, Lider (partner), Fecha Fin, Posiciones, etc.

Devuelve la misma estructura que leer_proyectos_mes() para ser compatible
con generar_excel.py y app_balance.py.
"""

import os
import sys
import re
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SGS UI"))

try:
    import openpyxl
except ImportError:
    print("ERROR: openpyxl no instalado. Ejecuta: pip install openpyxl")
    sys.exit(1)

import config as cfg

# ── Constantes de filtro ───────────────────────────────────────────────────────
REGION_OBJETIVO   = "UBATE"      # se normaliza (sin tilde, mayúsculas)
TECNOLOGIAS_OK    = {"MAYOR IMPACTO", "AMP ELECTRICO"}

# Nombres posibles de cada columna (el reporte puede variar entre versiones)
_ALIAS_REGION     = {"región", "region"}
_ALIAS_PROYECTO   = {"projecto", "proyecto", "project"}
_ALIAS_TECNOLOGIA = {"projecto/tecnología", "projecto/tecnologia",
                     "proyecto/tecnología", "proyecto/tecnologia",
                     "technology", "tecnología", "tecnologia"}
_ALIAS_FECHA_AV   = {"fecha avance", "fecha_avance", "advance date"}
_ALIAS_PRODUCCION = {"producción total", "produccion total",
                     "production total", "produccion_total"}
_ALIAS_LIDER      = {"lider", "partner", "líder"}
# NOTA: "ejecutor" NO está aquí — en el reporte, Ejecutor = el liquidador,
# NO el líder del proyecto. El líder real viene de partner_id en SGS.
_ALIAS_EJECUTOR_ID= {"ejecutor/númerodeidentificación",
                     "ejecutor/numerodeidentificacion",
                     "ejecutor/númerodeidentificación"}


def _normalizar(s: str) -> str:
    """Quita tildes, pasa a mayúsculas, quita espacios extra."""
    if not s:
        return ""
    REEMPLAZOS = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return str(s).translate(REEMPLAZOS).upper().strip()


def _encontrar_col(headers: list, aliases: set) -> int | None:
    """Devuelve el índice (0-based) de la primera columna que coincida con aliases."""
    for i, h in enumerate(headers):
        if _normalizar(h) in {_normalizar(a) for a in aliases}:
            return i
    return None


def _parse_fecha(raw) -> datetime | None:
    """Parsea fecha en formato Excel (datetime), string 'YYYY-MM-DD' o similar."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    s = str(raw).strip()
    for fmt, largo in [("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10),
                       ("%d/%m/%Y", 10), ("%d-%m-%Y", 10)]:
        try:
            return datetime.strptime(s[:largo], fmt)
        except ValueError:
            continue
    return None


def _extraer_pro_code(texto: str) -> str:
    """Extrae 'PRO-XXXXXX' de un texto como '[PRO-052835] Nombre del proyecto'."""
    if not texto:
        return ""
    m = re.search(r"PRO-\d+", str(texto), re.IGNORECASE)
    if m:
        return m.group(0).upper()
    # Si no tiene formato PRO-, devolver el texto limpio como código
    m2 = re.match(r"\[([^\]]+)\]", str(texto))
    return m2.group(1).strip() if m2 else str(texto).strip()


def _extraer_pdl(texto: str) -> str:
    """Extrae el código PDL/OT del nombre del proyecto."""
    if not texto:
        return ""
    s = str(texto)
    # Patrón DF20... o CDOPP...
    m = re.search(r"(DF\d+|CDOPP\w*|CDOPPI\w*|CDOPT\w*)", s, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Buscar en nombre después del código PRO
    partes = s.split("//")
    if len(partes) > 1:
        return partes[1].strip()
    return ""


def _proceso_desde_tecnologia(tech: str, pdl: str = "") -> str:
    """MI = Mayor Impacto; PG = AMP ELECTRICO (programados)."""
    t = _normalizar(tech)
    if "MAYOR IMPACTO" in t:
        return "MI"
    if "AMP ELECTRICO" in t or "ELECTRICO" in t:
        return "PG"
    pdl_up = (pdl or "").upper()
    if pdl_up.startswith("DF"):
        return "MI"
    return "PG"


# ── Conexión SGS (para resolver supervisor) ────────────────────────────────────

def _conectar():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "SGS UI"))
    from sgs_readonly_client import SGSReadOnlyClient
    db = SGSReadOnlyClient.detectar_db(cfg.SGS_URL)
    c  = SGSReadOnlyClient(cfg.SGS_URL, db, cfg.SGS_LOGIN, cfg.SGS_PASSWORD)
    c.autenticar()
    return c


def _resolver_supervisor(responsible_raw) -> str:
    """Convierte responsible_id de project.dominion al nombre del supervisor del equipo."""
    if not responsible_raw:
        return "?"
    if isinstance(responsible_raw, (list, tuple)) and len(responsible_raw) > 1:
        nombre_sgs = str(responsible_raw[1]).upper().strip()
    else:
        nombre_sgs = str(responsible_raw).upper().strip()

    for sup in cfg.SUPERVISORES_EQUIPO:
        if sup.upper() == nombre_sgs or sup.upper() in nombre_sgs:
            return sup
    palabras_sgs = set(nombre_sgs.split())
    for sup in cfg.SUPERVISORES_EQUIPO:
        for palabra_sup in sup.upper().split():
            if palabra_sup in palabras_sgs:
                return sup
    for clave, valor in cfg.MAPA_SUPERVISOR.items():
        if clave.upper() in nombre_sgs:
            return valor
    return nombre_sgs


def _leer_supervisores_sgs(pro_ids: list, verbose: bool = True) -> dict:
    """
    Consulta project.dominion para cada pro_id y devuelve {pro_id: supervisor_nombre}.
    Si falla la conexión, devuelve {} y el llamador asigna "?" a todos.
    """
    if not pro_ids:
        return {}
    try:
        c = _conectar()
        CAMPOS = ["id", "responsible_id", "code", "display_name", "partner_id"]
        resultado = {}
        for pro_id in pro_ids:
            try:
                recs = c.read("project.dominion", [pro_id], CAMPOS)
                if recs:
                    resultado[pro_id] = _resolver_supervisor(recs[0].get("responsible_id"))
                else:
                    resultado[pro_id] = "?"
            except Exception:
                resultado[pro_id] = "?"
        if verbose:
            print(f"      Supervisores resueltos vía SGS: {len(resultado)}")
        return resultado
    except Exception as e:
        if verbose:
            print(f"      AVISO: No se pudo conectar a SGS para resolver supervisores — {e}")
        return {}


def _buscar_pro_id_por_codigo(c, pro_code: str) -> int | None:
    """Busca el ID numérico de un proyecto en SGS por su código PRO-XXXXXX."""
    try:
        recs = c.search_read(
            "project.dominion",
            [["code", "=", pro_code]],
            ["id", "code"],
            limit=1,
        )
        return recs[0]["id"] if recs else None
    except Exception:
        return None


# ── Función principal ──────────────────────────────────────────────────────────

def _rango_mes(año: int, mes: int):
    inicio = datetime(año, mes, 1)
    fin    = datetime(año + 1, 1, 1) if mes == 12 else datetime(año, mes + 1, 1)
    return inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")


def _buscar_reporte(carpeta_input: str) -> str | None:
    """Busca el archivo de reporte de producción más reciente en carpeta_input."""
    if not os.path.isdir(carpeta_input):
        return None
    candidatos = []
    for nombre in os.listdir(carpeta_input):
        if nombre.lower().endswith(".xlsx") or nombre.lower().endswith(".xls"):
            ruta = os.path.join(carpeta_input, nombre)
            candidatos.append((os.path.getmtime(ruta), ruta))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    return candidatos[0][1]


def leer_reporte_produccion(
    año: int = None,
    mes: int = None,
    ruta_excel: str = None,
    verbose: bool = True,
) -> list:
    """
    Lee el Informe de Producción de SGS (Excel) y devuelve la lista de proyectos
    filtrada por Ubaté + Mayor Impacto + AMP Eléctrico para el mes indicado.

    Args:
        año, mes   : Período objetivo (por defecto mes actual)
        ruta_excel : Ruta al .xlsx; si None busca en BALANCE FINAL SGS/input/
        verbose    : Imprime progreso en consola

    Returns:
        Lista de dicts compatibles con generar_excel.py
    """
    hoy = datetime.now()
    if año is None: año = hoy.year
    if mes  is None: mes  = hoy.month

    MESES_ES = {1:"ENERO",2:"FEBRERO",3:"MARZO",4:"ABRIL",5:"MAYO",
                6:"JUNIO",7:"JULIO",8:"AGOSTO",9:"SEPTIEMBRE",
                10:"OCTUBRE",11:"NOVIEMBRE",12:"DICIEMBRE"}

    if verbose:
        print(f"\n[REPORTE] Leyendo Informe de Producción — {MESES_ES[mes]} {año}")

    # ── Localizar el archivo ───────────────────────────────────────────────────
    if ruta_excel is None:
        carpeta_input = os.path.join(os.path.dirname(__file__), "input")
        ruta_excel = _buscar_reporte(carpeta_input)
        if ruta_excel is None:
            raise FileNotFoundError(
                f"No se encontró ningún .xlsx en '{carpeta_input}'.\n"
                "Descarga el Informe de Producción desde SGS y colócalo en esa carpeta."
            )

    if verbose:
        print(f"      Archivo: {os.path.basename(ruta_excel)}")

    # ── Cargar Excel ───────────────────────────────────────────────────────────
    wb = openpyxl.load_workbook(ruta_excel, read_only=True, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        raise ValueError("El archivo Excel está vacío.")

    # ── Detectar fila de encabezados ───────────────────────────────────────────
    header_row_idx = None
    headers = []
    for i, row in enumerate(rows):
        row_vals = [str(c).strip() if c is not None else "" for c in row]
        # La fila de encabezados contiene "Región" o "Region"
        row_norm = [_normalizar(v) for v in row_vals]
        if any(a in row_norm for a in ["REGION", "REGION"]) and \
           any(a in row_norm for a in ["PROJECTO", "PROYECTO"]):
            header_row_idx = i
            headers = row_vals
            break

    if header_row_idx is None:
        raise ValueError(
            "No se encontró la fila de encabezados en el Excel.\n"
            "Verifica que el archivo sea el Informe de Producción de SGS."
        )

    if verbose:
        print(f"      Encabezados detectados en fila {header_row_idx + 1}: {headers[:6]}…")

    # ── Mapear columnas ────────────────────────────────────────────────────────
    col_region   = _encontrar_col(headers, _ALIAS_REGION)
    col_proyecto = _encontrar_col(headers, _ALIAS_PROYECTO)
    col_tecnol   = _encontrar_col(headers, _ALIAS_TECNOLOGIA)
    col_fecha_av = _encontrar_col(headers, _ALIAS_FECHA_AV)
    col_produccion = _encontrar_col(headers, _ALIAS_PRODUCCION)
    col_lider    = _encontrar_col(headers, _ALIAS_LIDER)

    faltantes = []
    if col_region    is None: faltantes.append("Región")
    if col_proyecto  is None: faltantes.append("Projecto")
    if col_tecnol    is None: faltantes.append("Projecto/Tecnología")
    if col_fecha_av  is None: faltantes.append("Fecha Avance")
    if col_produccion is None: faltantes.append("Producción total")

    if faltantes:
        raise ValueError(
            f"Columnas no encontradas en el Excel: {faltantes}\n"
            f"Columnas disponibles: {headers}"
        )

    if verbose:
        print(f"      Columnas mapeadas: Región={col_region}, Projecto={col_proyecto}, "
              f"Tecnología={col_tecnol}, FechaAvance={col_fecha_av}, "
              f"Producción={col_produccion}")

    # ── Leer y filtrar filas ───────────────────────────────────────────────────
    # Acumular por proyecto: {pro_code: {sum_mo, fechas, lider, tecnologia}}
    acum = {}   # pro_code_raw -> dict

    n_leidas = 0
    n_filtradas = 0

    for row in rows[header_row_idx + 1:]:
        if all(c is None for c in row):
            continue

        region_val  = str(row[col_region]   or "").strip()
        proyecto_val = str(row[col_proyecto] or "").strip()
        tecnol_val  = str(row[col_tecnol]   or "").strip()
        fecha_val   = row[col_fecha_av]
        prod_val    = row[col_produccion]
        lider_val   = str(row[col_lider] or "") if col_lider is not None else ""

        n_leidas += 1

        # Filtro 1: Región Ubaté
        if REGION_OBJETIVO not in _normalizar(region_val):
            continue

        # Filtro 2: Tecnología Mayor Impacto o AMP Eléctrico
        tecnol_norm = _normalizar(tecnol_val)
        if not any(_normalizar(t) in tecnol_norm or tecnol_norm in _normalizar(t)
                   for t in TECNOLOGIAS_OK):
            continue

        # Filtro 3: Fecha Avance en el mes objetivo
        fecha = _parse_fecha(fecha_val)
        if fecha is None or fecha.year != año or fecha.month != mes:
            continue

        n_filtradas += 1

        # Valor monetario
        try:
            mo_val = float(prod_val or 0)
        except (TypeError, ValueError):
            mo_val = 0.0

        # Acumular
        if proyecto_val not in acum:
            acum[proyecto_val] = {
                "proyecto_raw": proyecto_val,
                "sum_mo":       0.0,
                "fechas":       [],
                "lider":        _normalizar(lider_val),
                "tecnologia":   tecnol_val,
                "n_avances":    0,
            }
        acum[proyecto_val]["sum_mo"]    += mo_val
        acum[proyecto_val]["n_avances"] += 1
        if fecha:
            acum[proyecto_val]["fechas"].append(fecha)
        if not acum[proyecto_val]["lider"] and lider_val:
            acum[proyecto_val]["lider"] = _normalizar(lider_val)

    if verbose:
        print(f"      Filas leídas: {n_leidas} | Filtradas (Ubaté + MI/TP + mes): {n_filtradas}")
        print(f"      Proyectos únicos: {len(acum)}")

    if not acum:
        return []

    # ── Extraer códigos PRO y PDL ──────────────────────────────────────────────
    proyectos_raw = []
    for proyecto_raw, datos in acum.items():
        pro_code = _extraer_pro_code(proyecto_raw)
        pdl_code = _extraer_pdl(proyecto_raw)
        tecnol   = datos["tecnologia"]
        proceso  = _proceso_desde_tecnologia(tecnol, pdl_code)
        fechas   = sorted(datos["fechas"])
        fecha    = fechas[-1] if fechas else None

        proyectos_raw.append({
            "pro_code":    pro_code,
            "pro_id":      None,   # se rellena luego con SGS
            "proyecto_raw": proyecto_raw,
            "fecha":       fecha,
            "pdl_code":    pdl_code or pro_code,
            "lider":       datos["lider"],
            "lcl":         "N/A",
            "proceso":     proceso,
            "supervisor":  "?",    # se rellena luego
            "man_obra":    datos["sum_mo"],
            "estado":      "ok",
            "fuente_mo":   "reporte_produccion",
            "n_avances":   datos["n_avances"],
            "tecnologia":  tecnol,
        })

    # ── Resolver pro_id y supervisor vía SGS ───────────────────────────────────
    if verbose:
        print("      Resolviendo pro_id y supervisor en SGS...")

    try:
        c = _conectar()
        if verbose:
            print(f"      Autenticado: uid={c.uid}")

        for p in proyectos_raw:
            pro_code = p["pro_code"]
            if not pro_code:
                continue
            try:
                recs = c.search_read(
                    "project.dominion",
                    [["code", "=", pro_code]],
                    ["id", "code", "responsible_id", "partner_id",
                     "pdl", "odm", "order_number", "nodo", "name", "parent_id"],
                    limit=1,
                )
                if recs:
                    rec = recs[0]
                    p["pro_id"] = rec["id"]
                    p["supervisor"] = _resolver_supervisor(rec.get("responsible_id"))

                    # Proyecto padre (para consolidar nodos del mismo DF)
                    parent_raw = rec.get("parent_id")
                    if isinstance(parent_raw, (list, tuple)) and len(parent_raw) > 1:
                        p["parent_code"] = _extraer_pro_code(str(parent_raw[1]))
                    else:
                        p["parent_code"] = None

                    # Refinar PDL desde campos SGS si está vacío
                    if not p["pdl_code"] or p["pdl_code"] == pro_code:
                        pdl_sgs = (
                            rec.get("pdl") or rec.get("odm") or
                            rec.get("order_number") or rec.get("nodo") or ""
                        )
                        if not pdl_sgs:
                            nombre = rec.get("name") or ""
                            partes = nombre.split("//")
                            pdl_sgs = partes[0].strip() if partes else ""
                        if pdl_sgs:
                            p["pdl_code"] = pdl_sgs

                    # Lider siempre viene de partner_id en SGS (más preciso que el reporte)
                    partner_raw = rec.get("partner_id")
                    if isinstance(partner_raw, (list, tuple)) and len(partner_raw) > 1:
                        p["lider"] = str(partner_raw[1]).upper()
                    elif isinstance(partner_raw, str) and partner_raw:
                        p["lider"] = partner_raw.upper()

                    # LCL = número 6300... desde odm/order_number o del pdl_code
                    if p["proceso"] == "PG":
                        lcl_raw = (rec.get("odm") or rec.get("order_number") or "").strip()
                        m_lcl = re.search(r'6300\d+', lcl_raw) or re.search(r'6300\d+', p.get("pdl_code", ""))
                        p["lcl"] = m_lcl.group(0) if m_lcl else (lcl_raw or "N/A")
                else:
                    if verbose:
                        print(f"        AVISO: {pro_code} no encontrado en SGS")
            except Exception as e:
                if verbose:
                    print(f"        Error leyendo {pro_code}: {e}")

    except Exception as e:
        if verbose:
            print(f"      AVISO: Conexión SGS falló ({e}) — supervisores quedan como '?'")

    # ── Consolidar nodos del mismo DF o mismo LCL ─────────────────────────────
    from collections import defaultdict

    def _clave_grupo(p):
        if p.get("proceso") == "MI":
            return ("DF",  p.get("pdl_code", p["pro_code"]))
        lcl = p.get("lcl", "N/A")
        if lcl != "N/A" and lcl:
            return ("LCL", lcl)
        return ("PRO", p["pro_code"])

    grupos_pdl: dict = defaultdict(list)
    for p in proyectos_raw:
        grupos_pdl[_clave_grupo(p)].append(p)

    proyectos_consolidados = []
    for pdl, grupo in grupos_pdl.items():
        if len(grupo) == 1:
            proyectos_consolidados.append(grupo[0])
        else:
            base = max(grupo, key=lambda p: abs(p["man_obra"]))
            merged = base.copy()
            merged["man_obra"]  = sum(p["man_obra"] for p in grupo)
            merged["n_avances"] = sum(p["n_avances"] for p in grupo)
            fechas_v = [p["fecha"] for p in grupo if p["fecha"]]
            merged["fecha"] = max(fechas_v) if fechas_v else None
            parent_codes = {p.get("parent_code") for p in grupo if p.get("parent_code")}
            if len(parent_codes) == 1:
                merged["pro_code"] = parent_codes.pop()
            proyectos_consolidados.append(merged)
    proyectos_raw = proyectos_consolidados

    # ── Filtrar: solo proyectos del equipo liquidador ─────────────────────────
    # El reporte incluye MAYOR IMPACTO de toda la empresa; se descartan los que
    # no pertenecen a ningún supervisor del equipo (p. ej. los de Línea Viva).
    n_antes = len(proyectos_raw)
    proyectos_raw = [p for p in proyectos_raw if p["supervisor"] in cfg.SUPERVISORES_EQUIPO]
    if verbose and len(proyectos_raw) < n_antes:
        print(f"      Descartados {n_antes - len(proyectos_raw)} proyectos fuera del equipo "
              f"(Línea Viva u otros)")

    # ── Enriquecer: quién liquidó cada proyecto en el mes ────────────────────
    # Consulta batch de project.activities.details para los pro_ids conocidos
    inicio_str, fin_str = _rango_mes(año, mes)
    pro_ids_conocidos = [p["pro_id"] for p in proyectos_raw if p.get("pro_id")]

    liquidadores_por_pro: dict = {}   # pro_id -> {uid: {"nombre": ..., "mo": ...}}

    if pro_ids_conocidos:
        try:
            avances_todos = c.search_read(
                "project.activities.details",
                [["project_id", "in", pro_ids_conocidos],
                 ["date", ">=", inicio_str],
                 ["date", "<",  fin_str]],
                ["project_id", "create_uid", "advanced_price"],
                limit=5000,
            )
            for av in avances_todos:
                pid_raw = av.get("project_id")
                pro_id = pid_raw[0] if isinstance(pid_raw, (list, tuple)) else int(pid_raw)
                uid_raw = av.get("create_uid")
                if not uid_raw:
                    continue
                uid    = uid_raw[0] if isinstance(uid_raw, (list, tuple)) else uid_raw
                nombre = uid_raw[1] if isinstance(uid_raw, (list, tuple)) and len(uid_raw) > 1 else str(uid)
                # Limpiar nombre (quitar espacios extra)
                nombre = " ".join(nombre.split()).upper()
                mo_av  = float(av.get("advanced_price") or 0)

                if pro_id not in liquidadores_por_pro:
                    liquidadores_por_pro[pro_id] = {}
                if uid not in liquidadores_por_pro[pro_id]:
                    liquidadores_por_pro[pro_id][uid] = {"nombre": nombre, "mo": 0.0}
                liquidadores_por_pro[pro_id][uid]["mo"] += mo_av

            if verbose:
                print(f"      Liquidadores identificados en {len(liquidadores_por_pro)} proyectos")
        except Exception as e:
            if verbose:
                print(f"      AVISO: No se pudo consultar liquidadores — {e}")

    # Asignar liquidadores a cada proyecto
    for p in proyectos_raw:
        pro_id = p.get("pro_id")
        liqs = liquidadores_por_pro.get(pro_id, {})
        if liqs:
            # Ordenar por MO descendente; el que más liquidó va primero
            liqs_sorted = sorted(liqs.values(), key=lambda x: x["mo"], reverse=True)
            p["liquidadores"] = liqs_sorted   # lista de {nombre, mo}
            p["liquidado_por"] = " / ".join(l["nombre"].split()[0] for l in liqs_sorted)
        else:
            p["liquidadores"] = []
            p["liquidado_por"] = "?"

    # ── Ordenar por fecha ──────────────────────────────────────────────────────
    proyectos_raw.sort(key=lambda p: (p["fecha"] or datetime.min))

    if verbose:
        total = sum(p["man_obra"] for p in proyectos_raw)
        print(f"\n      {'FECHA':<12}  {'COD SGS':<16}  {'PROC':<4}  {'SUPERVISOR':<14}  {'LIQUIDADO POR':<25}  {'$ MAN OBRA':>18}")
        print(f"      {'─'*100}")
        for p in proyectos_raw:
            f   = p["fecha"].strftime("%Y-%m-%d") if p["fecha"] else "sin fecha"
            liqs = ", ".join(
                f"{l['nombre'].split()[0]} ${l['mo']:,.0f}" for l in p.get("liquidadores", [])
            ) or p.get("liquidado_por", "?")
            print(f"      {f:<12}  {p['pro_code']:<16}  {p['proceso']:<4}  "
                  f"{p['supervisor']:<14}  {liqs:<25}  ${p['man_obra']:>17,.2f}")
        print(f"      {'─'*100}")
        print(f"      {'TOTAL':<12}  {'':16}  {'':4}  {'':14}  {'':25}  ${total:>17,.2f}")
        print(f"\n      Proyectos: {len(proyectos_raw)}  |  Total MO: ${total:,.2f}")

    return proyectos_raw


# ─────────────────────────────────────────────────────────────────────────────
# Ejecución directa
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    from datetime import datetime

    print("\n" + "="*65)
    print("  LECTOR DE INFORME DE PRODUCCIÓN — BALANCE FINAL SGS")
    print("="*65)

    proyectos = leer_reporte_produccion(verbose=True)
    if not proyectos:
        print("\n  Sin datos. Verifica el archivo en la carpeta 'input/'")
    else:
        total = sum(p["man_obra"] for p in proyectos)
        print(f"\n  RESUMEN FINAL: {len(proyectos)} proyectos  |  Total MO = ${total:,.2f}")
