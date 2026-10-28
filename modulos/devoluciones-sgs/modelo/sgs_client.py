"""
Cliente SGS (Odoo JSON-RPC) - Solo lectura.
"""
import json
import re
import urllib.request
import urllib.error
import http.cookiejar


class SGSClient:
    MODELOS_BLOQUEADOS = {
        "account.move", "account.move.line", "account.journal",
        "account.account", "account.tax",
    }

    def __init__(self, url, db, login, password):
        self.url      = url.rstrip("/")
        self.db       = db
        self.login    = login
        self.password = password
        self.uid      = None
        # CookieJar maneja session_id automaticamente entre requests
        self._jar     = http.cookiejar.CookieJar()
        self._opener  = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar)
        )

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def _post(self, endpoint, payload):
        data    = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept":       "application/json",
            "User-Agent":   (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        req = urllib.request.Request(
            self.url + endpoint, data=data, headers=headers, method="POST"
        )
        with self._opener.open(req, timeout=20) as resp:
            raw    = resp.read().decode("utf-8")
            result = json.loads(raw)
            if "error" in result:
                msg = result["error"].get("data", {}).get("message", str(result["error"]))
                raise RuntimeError(f"SGS API error: {msg}")
            return result.get("result")

    def _call_kw(self, model, method, args, kwargs=None):
        return self._post("/web/dataset/call_kw", {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {
                "model":  model,
                "method": method,
                "args":   args,
                "kwargs": kwargs or {},
            },
        })

    # ── Auth ─────────────────────────────────────────────────────────────────

    @staticmethod
    def detectar_db(url):
        try:
            data = json.dumps({"jsonrpc": "2.0", "method": "call", "id": 0, "params": {}}).encode()
            req  = urllib.request.Request(
                url.rstrip("/") + "/web/database/list",
                data=data,
                headers={"Content-Type": "application/json",
                         "User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                r = json.loads(resp.read().decode())
                dbs = r.get("result", [])
                return dbs[0] if dbs else None
        except Exception:
            return None

    def autenticar(self):
        result = self._post("/web/session/authenticate", {
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"db": self.db, "login": self.login, "password": self.password},
        })
        if not result or not result.get("uid"):
            raise RuntimeError("Login fallido — verifica credenciales y DB.")
        self.uid = result["uid"]
        return self.uid

    # ── Helpers de lectura ────────────────────────────────────────────────────

    def search_read(self, model, domain, fields, limit=200, context=None):
        kwargs = {"fields": fields, "limit": limit}
        if context:
            kwargs["context"] = context
        return self._call_kw(model, "search_read", [domain], kwargs)

    def read(self, model, ids, fields=None, context=None):
        kwargs = {"fields": fields or []}
        if context:
            kwargs["context"] = context
        return self._call_kw(model, "read", [ids], kwargs)

    # ── Proyecto ──────────────────────────────────────────────────────────────

    # Campos seguros para search_read (sin computados que fallen en multi-registro)
    CAMPOS_BUSQUEDA = [
        "id", "name", "code", "display_name",
        "nodo", "pdl", "odm", "order_number", "circuito", "ticket",
        "status", "region_id", "macro_id", "micro_id",
        "responsible_id", "partner_id", "department_id",
        "date_assignment", "date_real", "date_end_real", "certification_date",
        "contains_subprojects", "parent_id", "project_count",
        "formula_id", "contract_id", "customer_id",
        "create_date", "write_date",
    ]

    # Campos extra solo seguros en read() de un registro individual
    CAMPOS_DETALLE = [
        "material_count", "tray_count", "pending_materials_count",
        "total_activity_points", "total_advanced_price", "amount_to_invoice",
        "ready_to_invoice", "has_pending_materials",
    ]

    def leer_proyecto(self, project_id):
        campos = self.CAMPOS_BUSQUEDA + self.CAMPOS_DETALLE
        r = self.read("project.dominion", [int(project_id)], campos)
        return r[0] if r else None

    def buscar_por_codigo(self, codigo):
        """Busca un proyecto por su codigo PRO-XXXXXX o por order_number."""
        codigo = str(codigo).strip()
        if codigo.upper().startswith("PRO-"):
            domain = [["code", "=", codigo.upper()]]
        else:
            domain = [["order_number", "=", codigo], ["parent_id", "=", False]]
        r = self.search_read("project.dominion", domain, self.CAMPOS_BUSQUEDA, limit=5)
        return r[0] if r else None

    # Campos minimos para search_read multi-registro (sin computados)
    CAMPOS_SEARCH_MINIMAL = ["id", "name", "code", "parent_id"]

    # Campos seguros para read() en batch (sin computados que fallen en multi-record)
    CAMPOS_BATCH = [
        "id", "name", "code",
        "nodo", "pdl", "odm", "order_number", "circuito", "ticket",
        "region_id", "macro_id", "micro_id",
        "responsible_id", "department_id",
        "formula_id", "contract_id", "customer_id",
        "date_assignment", "date_real", "date_end_real", "certification_date",
        "parent_id", "create_date", "write_date",
    ]

    def obtener_nodos_hijo(self, parent_id):
        """Retorna todos los proyectos hijo de un padre con datos completos."""
        hijos_ids = self.search_read(
            "project.dominion",
            [["parent_id", "=", int(parent_id)]],
            self.CAMPOS_SEARCH_MINIMAL,
            limit=200,
        )
        if not hijos_ids:
            return []
        # Leer campos basicos de todos en un solo llamado (sin computados)
        ids = [h["id"] for h in hijos_ids]
        basicos = self.read("project.dominion", ids, self.CAMPOS_BATCH)
        # Leer campos computados uno a uno
        extras = self.CAMPOS_BUSQUEDA + self.CAMPOS_DETALLE
        extras_seguros = [f for f in extras if f not in self.CAMPOS_BATCH]
        detalle_idx = {}
        for nid in ids:
            det = self.read("project.dominion", [nid], extras_seguros)
            if det:
                detalle_idx[nid] = det[0]
        for b in basicos:
            b.update(detalle_idx.get(b["id"], {}))
        return basicos

    # ── Bandejas ──────────────────────────────────────────────────────────────

    CAMPOS_BANDEJA = [
        "id", "name", "project_id", "date", "state",
        "partner_id", "responsible_id", "picking_id",
        "state_picking", "type", "notes",
        "project_tray_material_ids",
        "create_date",
    ]

    def obtener_bandejas(self, project_id):
        return self.search_read(
            "project.tray",
            [["project_id", "=", int(project_id)]],
            self.CAMPOS_BANDEJA,
            limit=100,
        )

    # ── Materiales de bandeja ─────────────────────────────────────────────────

    CAMPOS_MAT = [
        "id", "product_id", "product_uom_id",
        "deliver_quantity", "amount", "amount_limit",
        "project_tray_id", "project_id",
        "date", "project_tray_state", "picking_state",
        "is_customer_material",
    ]

    def obtener_materiales(self, mat_ids):
        if not mat_ids:
            return []
        return self.read("project.tray.material", mat_ids, self.CAMPOS_MAT)

    # ── Materiales confirmados (liquidacion) ─────────────────────────────────

    CAMPOS_PROJ_MAT = [
        "id", "product_id", "uom_id",
        "amount",      # proyectado
        "tray",        # total bandejas deliver (sin descontar devoluciones)
        "despachado",  # neto almacen = tray_deliver - tray_devolucion
        "advance",     # CONSUMIDO = lo que el usuario liquida manualmente  ← clave
        "balance",     # pendiente de liquidar = despachado - advance
        "diferencia",  # diferencia almacen = tray - despachado (devoluciones)
        "is_pending",
        "is_customer_material",
        "customer_default_code",
        "write_uid", "write_date",
    ]

    def obtener_materiales_liquidacion(self, project_id):
        """Lee project.material — lo que el usuario confirmó para liquidar."""
        return self.search_read(
            "project.material",
            [["project_id", "=", int(project_id)]],
            self.CAMPOS_PROJ_MAT,
            limit=300,
        )

    # ── Detalles de consumo por lider ────────────────────────────────────────

    CAMPOS_DETALLE_LIQ = [
        "id", "product_id", "amount", "technical_stock",
        "partner_id", "worker_id", "state", "date",
        "project_material_id", "lot_id",
    ]

    def obtener_detalles_liquidacion(self, project_id):
        """Lee project.material.details — consumos por lider/tecnico con stock."""
        return self.search_read(
            "project.material.details",
            [["project_id", "=", int(project_id)]],
            self.CAMPOS_DETALLE_LIQ,
            limit=500,
        )

    # ── Balance completo ──────────────────────────────────────────────────────

    def balance_completo(self, parent_id):
        """
        Retorna dict con:
          padre   : datos del proyecto padre
          nodos   : lista de nodos hijo, cada uno con clave 'bandejas'
                    y cada bandeja con clave 'materiales'
        """
        padre = self.leer_proyecto(parent_id)
        if not padre:
            raise RuntimeError(f"Proyecto {parent_id} no encontrado.")

        nodos = self.obtener_nodos_hijo(parent_id)
        # Si no tiene hijos, puede ser un nodo simple
        if not nodos:
            nodos = [padre]

        for nodo in nodos:
            bandejas = self.obtener_bandejas(nodo["id"])
            for ban in bandejas:
                mat_ids = ban.get("project_tray_material_ids") or []
                ban["materiales"] = self.obtener_materiales(mat_ids)
            nodo["bandejas"]        = bandejas
            nodo["liq_materiales"]  = self.obtener_materiales_liquidacion(nodo["id"])
            nodo["liq_detalles"]    = self.obtener_detalles_liquidacion(nodo["id"])

        return {"padre": padre, "nodos": nodos}


# ── Utilidades de parseo ──────────────────────────────────────────────────────

def parsear_producto(product_field):
    """
    product_field = [id, '[DOM-028733-12/274287] ABRAZADERA...']
    Retorna (codigo_dom, codigo_alt, descripcion)
    """
    if not product_field:
        return "", "", ""
    texto = product_field[1] if isinstance(product_field, list) else str(product_field)
    m = re.match(r"\[([^\]/]+)(?:/([^\]]+))?\]\s*(.*)", texto)
    if m:
        return m.group(1).strip(), (m.group(2) or "").strip(), m.group(3).strip()
    return "", "", texto


def nombre_campo(campo):
    """Extrae el nombre de un campo Many2one: [id, 'nombre'] -> 'nombre'"""
    if isinstance(campo, list) and len(campo) >= 2:
        return campo[1]
    return campo or ""
