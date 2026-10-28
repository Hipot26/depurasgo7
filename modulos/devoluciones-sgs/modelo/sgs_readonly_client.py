"""
SGSReadOnlyClient — Cliente de SOLO LECTURA para SGS (Odoo XML-RPC)
====================================================================
CONTRATO: Este módulo JAMÁS escribe, crea, modifica ni elimina datos en SGS.
Cualquier intento de escritura lanza ReadOnlyViolationError en tiempo de
ejecución, antes de que la petición salga de esta máquina.

Métodos permitidos (lectura pura):
  search_read, read, search, fields_get, name_get, name_search
  buscar_por_codigo, detectar_db

Métodos bloqueados (bloqueo explícito — falla ruidosamente):
  create, write, unlink, execute_kw(*), copy, action_*, button_*,
  y cualquier método no incluido en la lista blanca.
"""

import sys
import os
import logging
from datetime import datetime

# ── Apuntar al SGSClient original (solo para heredar la conexión) ──────────────
# En el server, sgs_client.py va en la MISMA carpeta (bundle). En local vive en
# ../SGS + SONY/sgs_api. Se insertan ambas rutas; gana la que tenga el archivo.
_HERE = os.path.dirname(os.path.abspath(__file__))
_SGS_API_DIR = os.path.join(_HERE, "..", "SGS + SONY", "sgs_api")
for _p in (_HERE, _SGS_API_DIR):
    sys.path.insert(0, _p)
from sgs_client import SGSClient  # solo hereda autenticar / _post / _opener

log = logging.getLogger("sgs_readonly")

# ── Métodos permitidos en el ORM de Odoo ──────────────────────────────────────
_METODOS_LECTURA = frozenset([
    "search_read",
    "read",
    "search",
    "fields_get",
    "name_get",
    "name_search",
    "read_group",
    "get_views",
    "get_view",
    "onchange",          # solo calcula valores, no persiste
    "check_access_rights",
])

# ── Cualquier prefijo de método que huela a escritura ─────────────────────────
_PREFIJOS_PELIGROSOS = (
    "create", "write", "unlink", "delete",
    "action_", "button_", "do_", "post_",
    "confirm", "validate", "cancel", "approve",
    "toggle_", "set_", "mark_", "_apply",
)


class ReadOnlyViolationError(RuntimeError):
    """Se lanza cuando algo intenta escribir datos via este cliente."""
    pass


def _guardar_log_lectura(model: str, method: str, domain):
    """Registra cada lectura en un archivo local para auditoría."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "reads.log"), "a", encoding="utf-8") as f:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"{ts} | {model}.{method} | {str(domain)[:120]}\n")


class SGSReadOnlyClient:
    """
    Cliente SGS de solo lectura.

    Uso:
        c = SGSReadOnlyClient(URL, db, LOGIN, PASSWORD)
        c.autenticar()
        datos = c.search_read("project.project", [...], [...])

    Cualquier intento de llamar a create/write/unlink/etc.
    lanza ReadOnlyViolationError ANTES de hacer la petición.
    """

    def __init__(self, url: str, db: str, login: str, password: str):
        self._inner = SGSClient(url, db, login, password)
        self.url      = url
        self.db       = db
        self.login    = login
        self._password = password
        self.uid      = None

    # ── Conexión ──────────────────────────────────────────────────────────────
    def autenticar(self):
        self._inner.autenticar()
        self.uid = self._inner.uid
        log.info(f"[READONLY] Autenticado uid={self.uid} db={self.db}")
        return self.uid

    @staticmethod
    def detectar_db(url: str) -> str:
        return SGSClient.detectar_db(url)

    # ── Guardián central ──────────────────────────────────────────────────────
    def _verificar_metodo(self, method: str):
        """Lanza si el método no está en la lista blanca o huele a escritura."""
        method_lower = method.lower()

        if method_lower in _METODOS_LECTURA:
            return  # OK

        for prefijo in _PREFIJOS_PELIGROSOS:
            if method_lower.startswith(prefijo) or method_lower == prefijo.rstrip("_"):
                raise ReadOnlyViolationError(
                    f"\n{'='*60}\n"
                    f"  BLOQUEO DE SEGURIDAD — SGSReadOnlyClient\n"
                    f"  Método bloqueado: '{method}'\n"
                    f"  Este cliente es estrictamente de SOLO LECTURA.\n"
                    f"  Para acciones de escritura usa PlaywrightClient.\n"
                    f"{'='*60}"
                )

        raise ReadOnlyViolationError(
            f"\n{'='*60}\n"
            f"  BLOQUEO DE SEGURIDAD — método desconocido: '{method}'\n"
            f"  Solo se permiten: {sorted(_METODOS_LECTURA)}\n"
            f"{'='*60}"
        )

    # ── Métodos de lectura expuestos ───────────────────────────────────────────
    _CTX = {"allowed_company_ids": [1]}

    def search_read(self, model: str, domain: list, fields: list,
                    limit: int = 100, offset: int = 0, order: str = None):
        self._verificar_metodo("search_read")
        _guardar_log_lectura(model, "search_read", domain)
        return self._inner.search_read(model, domain, fields, limit=limit, context=self._CTX)

    def read(self, model: str, ids: list, fields: list):
        self._verificar_metodo("read")
        _guardar_log_lectura(model, "read", ids)
        return self._inner.read(model, ids, fields, context=self._CTX)

    def search(self, model: str, domain: list, limit: int = 100):
        self._verificar_metodo("search")
        _guardar_log_lectura(model, "search", domain)
        return self._inner.search(model, domain, limit=limit)

    def fields_get(self, model: str, attributes: list = None):
        self._verificar_metodo("fields_get")
        _guardar_log_lectura(model, "fields_get", [])
        return self._inner.fields_get(model, attributes=attributes)

    def buscar_por_codigo(self, codigo: str):
        self._verificar_metodo("search_read")
        _guardar_log_lectura("project.project", "buscar_por_codigo", [codigo])
        return self._inner.buscar_por_codigo(codigo)

    # ── Bloqueos explícitos con mensajes claros ────────────────────────────────
    def create(self, *args, **kwargs):
        raise ReadOnlyViolationError(
            "create() está BLOQUEADO. Usa PlaywrightClient para crear registros.")

    def write(self, *args, **kwargs):
        raise ReadOnlyViolationError(
            "write() está BLOQUEADO. Usa PlaywrightClient para modificar registros.")

    def unlink(self, *args, **kwargs):
        raise ReadOnlyViolationError(
            "unlink() está BLOQUEADO. Eliminar registros no está permitido.")

    def execute_kw(self, *args, **kwargs):
        raise ReadOnlyViolationError(
            "execute_kw() directo está BLOQUEADO en el cliente de solo lectura.")

    def __getattr__(self, name: str):
        raise ReadOnlyViolationError(
            f"Método/atributo '{name}' no disponible en SGSReadOnlyClient.\n"
            f"Si necesitas lectura, verifica la lista blanca en sgs_readonly_client.py.\n"
            f"Si necesitas escritura, usa PlaywrightClient."
        )
