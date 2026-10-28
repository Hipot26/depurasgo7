"""sgs_cierre.py — Cierre mensual de SGS aprendido automáticamente (SGO7).

SGS rechaza los avances de mano de obra cuya fecha cae fuera del «rango de producción en
curso» (Odoo: «La fecha de avance esta fuera del rango de produccion en curso:
2026-10-03 - 2026-11-03»). Facturación fecha las planillas con el día real del trabajo, así
que al cerrar el mes las planillas de esos días chocan con el rango nuevo.

Regla SGO7: la primera vez que el Liquidador se topa con ese diálogo, aprende el rango y lo
guarda en un archivo compartido del servidor; desde ese momento, para TODOS los usuarios,
toda planilla con fecha anterior al inicio del rango se ajusta sola a `inicio + DIAS_MARGEN`
(1 día → p. ej. 2026-10-04). El límite caduca solo cuando termina el rango; el mes siguiente
se vuelve a aprender con el primer choque.

Todo cambio queda en Registros SGO7 (lo registran el Liquidador y browser_client).
"""
import json
import os
import re
import threading
from datetime import date, datetime, timedelta

RUTA = os.environ.get("SGO7_CIERRE_SGS", "/var/www/sgo7/registros/sgs_cierre.json")
DIAS_MARGEN = int(os.environ.get("SGO7_CIERRE_MARGEN_DIAS", "1"))
_RE = re.compile(r"fuera del rango de producci[oó]n en curso:?\s*(\d{4}-\d{2}-\d{2})\s*-\s*(\d{4}-\d{2}-\d{2})", re.I)
_lock = threading.Lock()


def detectar(texto):
    """Devuelve (rango_inicio, rango_fin) si el texto es el error de rango de SGS; si no, None."""
    m = _RE.search(texto or "")
    return (m.group(1), m.group(2)) if m else None


def cargar() -> dict:
    try:
        with open(RUTA, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def vigente():
    """El cierre aprendido, o None si no hay o ya caducó (hoy es posterior al fin del rango)."""
    d = cargar()
    if not d.get("rango_inicio"):
        return None
    if d.get("rango_fin") and date.today().isoformat() > d["rango_fin"]:
        return None
    return d


def fecha_minima():
    v = vigente()
    return v.get("fecha_minima") if v else None


def guardar(rango_inicio, rango_fin, error="", por=None, sid=None, captura=None) -> dict:
    """Guarda (o refresca) el cierre aprendido. Devuelve el registro guardado."""
    with _lock:
        prev = cargar()
        mismo = prev.get("rango_inicio") == rango_inicio
        d = {
            "rango_inicio": rango_inicio,
            "rango_fin": rango_fin,
            "fecha_minima": (date.fromisoformat(rango_inicio) + timedelta(days=DIAS_MARGEN)).isoformat(),
            "margen_dias": DIAS_MARGEN,
            "error": (error or "")[:300],
            "detectado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "detectado_por": por or prev.get("detectado_por") if mismo else por,
            "sesion": sid,
            "captura": captura,
            "veces": (prev.get("veces", 0) + 1) if mismo else 1,
            "primera_vez": prev.get("primera_vez") if mismo and prev.get("primera_vez") else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        os.makedirs(os.path.dirname(RUTA), exist_ok=True)
        tmp = RUTA + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, RUTA)
        try:
            os.chmod(RUTA, 0o640)
        except Exception:
            pass
        return d


def ajustar(fecha_iso):
    """(fecha_ajustada, info) — info es el cierre aplicado o None si no hubo que ajustar.
    fecha_iso en YYYY-MM-DD; se compara como texto (válido en ISO)."""
    v = vigente()
    if v and fecha_iso and len(fecha_iso) == 10 and fecha_iso < v["rango_inicio"]:
        return v["fecha_minima"], v
    return fecha_iso, None


def ajustar_ddmmyyyy(fecha_ddmmyyyy):
    """Misma regla para el formato DD/MM/YYYY que usa el modal de MO. None si no aplica."""
    try:
        if not fecha_ddmmyyyy:
            return None
        iso = datetime.strptime(fecha_ddmmyyyy, "%d/%m/%Y").strftime("%Y-%m-%d")
        nueva, info = ajustar(iso)
        if info and nueva != iso:
            return datetime.strptime(nueva, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        pass
    return None


def descripcion():
    """Texto corto para avisos: 'SGS cerró el periodo anterior: rango en curso 2026-10-03 – 2026-11-03 (detectado …)'."""
    v = vigente()
    if not v:
        return ""
    return (f"SGS solo acepta avances del rango en curso {v['rango_inicio']} – {v['rango_fin']}; "
            f"las planillas anteriores se registran con {v['fecha_minima']} "
            f"(cierre detectado el {v['detectado']}"
            + (f" por {v['detectado_por']}" if v.get("detectado_por") else "") + ").")
