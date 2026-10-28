"""Configuración común de las pruebas unitarias.

Las pruebas importan funciones puras de los módulos tal como están en el repositorio (sin instalación), así que se
añaden sus carpetas a `sys.path`. No hay red ni base de datos: las variables de entorno mínimas que exigen algunos
módulos al importarse se fijan aquí con valores sintéticos.

Orden de `sys.path`: la plataforma central va primero porque su paquete `app/` comparte nombre con el `app.py` de
los módulos Flask; después `sgs-ui` (aporta `browser_client`, `sgs_cierre`, `sgs_readonly_client`, `sgo7_registro`) y
`sony-sgs/nuevo` (aporta `balance_liquidar`, `integracion`).
"""
import json
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CARPETAS = [
    os.path.join(RAIZ, "sabogal-platform", "backend"),
    os.path.join(RAIZ, "sgs-ui"),
    os.path.join(RAIZ, "sony-sgs", "nuevo"),
]
for carpeta in reversed(CARPETAS):
    if carpeta in sys.path:
        sys.path.remove(carpeta)
    sys.path.insert(0, carpeta)

# Settings de la plataforma central: valores sintéticos, nunca una conexión real.
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://prueba:prueba@127.0.0.1:1/prueba")  # pragma: allowlist secret
os.environ.setdefault("SECRET_KEY", "clave-de-prueba")
os.environ.setdefault("ELABORADORES_JSON", json.dumps({
    "cuenta.a": {"nombre": "Elaborador A", "tokens": ["APELLIDOA", "NOMBREA"]},
    "cuenta.b": {"nombre": "Elaborador B", "tokens": ["APELLIDOB"]},
}))
os.environ.setdefault("USUARIOS_LIQUIDADOR", "cuenta.liq")
os.environ.setdefault("USUARIOS_NODOS", "cuenta.nodos, cuenta.directivo")

# Las copias de sgo7_registro.py envían a un backend local si existe; en pruebas no se envía nada.
os.environ.setdefault("SGO7_REGISTROS_URL", "http://127.0.0.1:1/registros")
os.environ.setdefault("SGO7_REGISTROS_FALLBACK", os.devnull)
os.environ.setdefault("SGO7_COPIAS_DIR", os.path.join(RAIZ, "tests", "_copias_prueba"))
