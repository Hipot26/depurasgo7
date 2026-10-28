"""
config.py — Configuración central del módulo de Devoluciones.

Todos los secretos y parámetros de la instalación se leen de variables de entorno
(.env del módulo, cargado por start.sh). Sin valores por defecto para credenciales,
URL ni base de SGS.
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))


def _bool(key: str, default: bool) -> bool:
    return os.getenv(key, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# ── Conexión SGS (cuenta de escritura de la zona base) ────────────────────────
SGS_URL  = os.getenv("SGS_URL",  "")
SGS_DB   = os.getenv("SGS_DB",   "")
SGS_USER = os.getenv("SGS_USER", "")
SGS_PASS = os.getenv("SGS_PASS", "")
# browser_client (Playwright) usa estos alias:
SGS_LOGIN    = os.getenv("SGS_LOGIN",    SGS_USER)
SGS_PASSWORD = os.getenv("SGS_PASSWORD", SGS_PASS)

# ── IA MiniMax (intérprete de órdenes) ────────────────────────────────────────
MINIMAX_KEY   = os.getenv("MINIMAX_KEY", "")
MINIMAX_URL   = os.getenv("MINIMAX_URL", "https://api.minimax.io/v1/text/chatcompletion_v2")
MINIMAX_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-Text-01")

# ── Login del panel web ───────────────────────────────────────────────────────
# REQUIRE_AUTH=0 en local (sin login). En el servidor start.sh pone REQUIRE_AUTH=1.
REQUIRE_AUTH  = _bool("REQUIRE_AUTH", False)
WEB_USER      = os.getenv("WEB_USER", "").strip().lower()
WEB_PASS      = os.getenv("WEB_PASS", "")        # texto plano (solo por variable de entorno)
WEB_PASS_HASH = os.getenv("WEB_PASS_HASH", "")   # alternativa: hash werkzeug (preferido)
SECRET_KEY    = os.getenv("APP_SECRET", "")
COOKIE_SECURE = _bool("COOKIE_SECURE", False)    # True en el servidor (HTTPS)

# Anti fuerza-bruta del login
LOGIN_MAX_INTENTOS = int(os.getenv("LOGIN_MAX_INTENTOS", "6"))
LOGIN_LOCKOUT_SEG  = int(os.getenv("LOGIN_LOCKOUT_SEG", "300"))

# ── Rutas / runtime ───────────────────────────────────────────────────────────
EXCEL_DIR    = os.getenv("EXCEL_DIR", os.path.join(_HERE, "IMPORTAR_NODOS"))
MAT_TEMPLATE = os.getenv("MAT_TEMPLATE", "")
PORT         = int(os.getenv("PORT", "47293"))
HOST         = os.getenv("HOST", "127.0.0.1")
