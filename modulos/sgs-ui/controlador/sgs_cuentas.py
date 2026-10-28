"""sgs_cuentas.py — Cuenta SGS de cada usuario de SGO7, para el Liquidador.

Las credenciales viven cifradas en la base de sabogal (tabla sgo7_credenciales_sgs) y el
módulo las pide al backend por el canal interno (127.0.0.1:3008, token SGO7_INTERNAL_TOKEN)
solo en el momento de abrir o renovar la sesión SGS de ese usuario. Nunca se guardan aquí.
"""
import json
import os
import urllib.parse
import urllib.request

BASE = os.environ.get("SGO7_API_INTERNA", "http://127.0.0.1:3008/api/sgo7/interno")
TOKEN = os.environ.get("SGO7_INTERNAL_TOKEN", "")


def _req(path, data=None, timeout=6):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode("utf-8") if data is not None else None,
                                 method="POST" if data is not None else "GET",
                                 headers={"X-SGO7-Interno": TOKEN, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def obtener(usuario):
    """{'sgs_login': ..., 'sgs_password': ...} o None si el usuario no registró cuenta."""
    try:
        return _req("/sgs-cuenta?usuario=" + urllib.parse.quote(usuario or ""))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception:
        return None


def reportar(usuario, ok, error=""):
    """Deja en la cuenta el resultado del último intento (se ve en «Mi cuenta SGS»)."""
    try:
        _req("/sgs-cuenta/estado", {"usuario": usuario, "ok": bool(ok), "error": (error or "")[:300]})
    except Exception:
        pass
