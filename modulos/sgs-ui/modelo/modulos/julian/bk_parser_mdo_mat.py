"""
parser_mdo_mat.py — Tabla de precios MDO&MAT (referencia interna).

Lee la hoja 'MDO&MAT' del Excel de presupuesto de referencia y construye
un diccionario {qp_code: {precio, unidad, descripcion}} usado para calcular
el valor monetario de cada prestación en la distribución por porcentaje.

El archivo de referencia es fijo. Se cachea en JSON para no reparsear
20 k filas en cada request.  Usar regenerar_desde_excel() para actualizar.
"""

import json
import logging
import os
from pathlib import Path

import openpyxl

log = logging.getLogger("mdo_mat")

_CACHE_JSON = Path(__file__).parent / "precios_mdo_mat.json"
# Excel de referencia del baremo (hoja MDO&MAT): ruta por la variable MDO_MAT_EXCEL; si no está, se
# espera junto a este módulo. Normalmente basta el caché JSON; el Excel solo se lee al regenerar.
_EXCEL_DEFAULT = Path(os.environ.get("MDO_MAT_EXCEL") or (Path(__file__).parent / "MDO_MAT.xlsx"))

_mem: dict | None = None


def get_precios(force_reload: bool = False) -> dict[str, dict]:
    """
    Retorna {qp_code: {precio, unidad, descripcion}}.
    Orden de preferencia: memoria → JSON cache → Excel.
    """
    global _mem
    if _mem is not None and not force_reload:
        return _mem
    if _CACHE_JSON.exists() and not force_reload:
        try:
            with open(_CACHE_JSON, encoding="utf-8") as f:
                _mem = json.load(f)
            log.info(f"[MDO&MAT] {len(_mem)} códigos cargados desde JSON cache")
            return _mem
        except Exception as ex:
            log.warning(f"[MDO&MAT] JSON cache inválido ({ex}) — recargando desde Excel")
    _mem = _parsear_excel(_EXCEL_DEFAULT)
    _guardar_json(_mem)
    return _mem


def regenerar_desde_excel(excel_path: str | Path | None = None) -> tuple[dict, int]:
    """
    Fuerza re-parseo desde Excel.  Retorna (precios_dict, n_codigos).
    Usar para el botón 'Actualizar precios MDO&MAT'.
    """
    global _mem
    path = Path(excel_path) if excel_path else _EXCEL_DEFAULT
    _mem = _parsear_excel(path)
    _guardar_json(_mem)
    log.info(f"[MDO&MAT] Regenerados {len(_mem)} códigos desde {path.name}")
    return _mem, len(_mem)


def info_cache() -> dict:
    """Devuelve metadatos del cache para mostrar en UI."""
    return {
        "json_existe":  _CACHE_JSON.exists(),
        "json_path":    str(_CACHE_JSON),
        "excel_path":   str(_EXCEL_DEFAULT),
        "excel_existe": _EXCEL_DEFAULT.exists(),
        "n_en_memoria": len(_mem) if _mem is not None else None,
    }


# ── Internos ──────────────────────────────────────────────────────────────────

def _parsear_excel(path: Path) -> dict:
    if not path.exists():
        log.error(f"[MDO&MAT] Archivo no encontrado: {path}")
        return {}
    try:
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    except Exception as ex:
        log.error(f"[MDO&MAT] No se pudo abrir Excel: {ex}")
        return {}
    if "MDO&MAT" not in wb.sheetnames:
        log.error(f"[MDO&MAT] Hoja 'MDO&MAT' no encontrada en {path.name}")
        wb.close()
        return {}
    ws = wb["MDO&MAT"]
    result = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or len(row) < 8:
            continue
        code = str(row[0] or "").strip().upper()
        if not code.startswith("QP"):
            continue
        precio = _pf(row[7]) or 0.0
        unidad = str(row[5] or "").strip()
        desc   = str(row[3] or "").strip()[:120]
        result[code] = {"precio": precio, "unidad": unidad, "descripcion": desc}
    wb.close()
    log.info(f"[MDO&MAT] Parseados {len(result)} códigos QP")
    return result


def _guardar_json(data: dict) -> None:
    try:
        with open(_CACHE_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as ex:
        log.warning(f"[MDO&MAT] No se pudo guardar JSON cache: {ex}")


def _pf(val) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("$", "").replace(" ", "").replace("\xa0", "").replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None
