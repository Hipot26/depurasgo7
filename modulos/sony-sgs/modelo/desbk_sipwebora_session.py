"""
Módulo de sesión autenticada SIPWEBORA.
Lee credenciales de config.json y mantiene la sesión activa.
"""

import json
import os
import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar

BASE     = "https://sipwebora.com/sonny"
LOGIN_URL = f"{BASE}/sip/validatelogin.php"
CONFIG   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CO,es;q=0.9",
}


def cargar_credenciales():
    if not os.path.exists(CONFIG):
        raise FileNotFoundError(f"No se encontró {CONFIG}")
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    u = cfg.get("usuario", "").strip()
    p = cfg.get("contrasena", "").strip()
    if not u or not p:
        raise ValueError(
            "Completa usuario y contrasena en config.json"
        )
    return u, p


class SipSession:
    """Sesión autenticada reutilizable."""

    def __init__(self):
        self.jar     = http.cookiejar.CookieJar()
        self.opener  = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self._logged = False

    def login(self, usuario=None, contrasena=None):
        if usuario is None:
            usuario, contrasena = cargar_credenciales()

        payload = urllib.parse.urlencode({
            "user":    usuario,
            "passw":   contrasena,
            "modulo":  "Sonny Web",
            "page":    "cpanel.php",
        }).encode()

        req = urllib.request.Request(LOGIN_URL, data=payload, headers={
            **HEADERS,
            "Content-Type":  "application/x-www-form-urlencoded",
            "Referer":       f"{BASE}/index.php",
        })

        try:
            resp = self.opener.open(req, timeout=15)
            html = resp.read().decode("utf-8", errors="replace")
            # Si redirige o contiene cpanel = login OK
            final_url = resp.geturl()
            if "cpanel" in final_url or "cpanel" in html.lower():
                self._logged = True
                print("[OK] Sesion iniciada correctamente.")
                return True
            elif "sesi" in html.lower() and "expir" in html.lower():
                print("[ERROR] Credenciales incorrectas.")
                return False
            else:
                # Login parece exitoso si no hay mensaje de error
                self._logged = True
                return True
        except urllib.error.URLError as e:
            print(f"[ERROR] No se pudo conectar: {e}")
            return False

    def get(self, url):
        """GET autenticado."""
        if not self._logged:
            self.login()
        req = urllib.request.Request(url, headers=HEADERS)
        resp = self.opener.open(req, timeout=20)
        return resp.read().decode("utf-8", errors="replace")

    def post(self, url, params: dict):
        """POST autenticado."""
        if not self._logged:
            self.login()
        payload = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=payload, headers={
            **HEADERS,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/cpanel.php",
        })
        resp = self.opener.open(req, timeout=20)
        return resp.read().decode("utf-8", errors="replace")
