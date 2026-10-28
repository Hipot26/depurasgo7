"""
PlaywrightClient — Automatización de navegador para SGS_UI_Materiales
======================================================================
ÚNICA clase autorizada para escribir datos en SGS.
Replica exactamente las acciones del usuario humano.

Métodos: login(), inspect_form() (mapea un formulario desconocido), crear_proyecto_hijo(),
crear_bandeja(), crear_devolucion() y los pasos de aprobación.

REGLA: ningún método de escritura se implementa sin haber completado la fase de
exploración del formulario y la fase de prueba en seco (dry_run).
"""

import logging
import os
import re
import sys
import time
import json
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PWTimeout

log = logging.getLogger("browser_client")

# ── Configuración (secretos centralizados en config.py / variables de entorno) ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD

WAIT_TIMEOUT = 60_000   # ms — timeout general para esperas de DOM
SLOW_MO_MS   = 100      # ms — pausa entre acciones (NO bajar de 100)


def _safe(s) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


class BrowserActionError(RuntimeError):
    pass


class PlaywrightClient:
    """
    Controla el navegador para ejecutar acciones en SGS.

    Uso de exploración (headless=False para ver la UI):
        with PlaywrightClient(headless=False, slow_mo=500) as pw:
            pw.login()
            info = pw.inspect_form("project.project", record_id="new")
            # → guarda HTML + campos en logs/

    Uso de automatización (headless=True):
        pw = PlaywrightClient(headless=True, slow_mo=100)
        pw.start()
        pw.login()
        # ... acciones de creación aquí
        pw.close()
    """

    def __init__(self, headless: bool = False, slow_mo: int = None,
                 sgs_login: str = None, sgs_password: str = None):
        self.headless  = headless
        self._slow_mo  = slow_mo if slow_mo is not None else SLOW_MO_MS
        # Cuenta con la que ESCRIBE este navegador. Por defecto la de config.py
        # (zona base); otra zona pasa la suya (devoluciones_sgs._cred_sid).
        self._sgs_login    = sgs_login or SGS_LOGIN
        self._sgs_password = sgs_password or SGS_PASSWORD
        self._pw       = None
        self._browser: Browser = None
        self._page: Page = None
        self._logueado = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.close()

    # ── Ciclo de vida ─────────────────────────────────────────────────────────
    def start(self, storage_state=None):
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self._slow_mo,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx_opts = {"base_url": SGS_URL}
        if storage_state:
            ctx_opts["storage_state"] = storage_state
        ctx = self._browser.new_context(**ctx_opts)
        self._page = ctx.new_page()
        if storage_state:
            self._logueado = True
            log.info("[PW] Navegador iniciado con sesión heredada (sin login)")

    def close(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        finally:
            self._browser = None
            self._pw      = None
            self._page    = None
            self._logueado = False

    # ── Login ─────────────────────────────────────────────────────────────────
    def login(self, intentos: int = 3):
        """Login con reintento. Entrar no escribe nada en SGS, así que reintentar es
        seguro (la regla «sin reintento tras guardar» solo aplica DESPUÉS de GUARDAR). Visto 2026-09-11: en
        1 de ~64 logins el authenticate devolvió uid pero /web?cids=1 rebotó a
        /web/login, y ese navegador se llevó sus 4 hojas a "error".
        Credenciales rechazadas NO se reintentan: Odoo bloquea el usuario tras
        varios fallos seguidos (login_cooldown) y con 2 navegadores se llega rápido."""
        ultimo = None
        for n in range(1, intentos + 1):
            try:
                self._login_una_vez()
                return
            except BrowserActionError as e:
                if "rechazó las credenciales" in str(e):
                    raise
                ultimo = e
            except Exception as e:                  # timeouts de red/navegación
                ultimo = e
            log.warning(f"[LOGIN] intento {n}/{intentos} falló: {ultimo}")
            if n < intentos:
                try:
                    self._page.context.clear_cookies()   # arrancar con sesión limpia
                except Exception:
                    pass
                time.sleep(3 * n)
        raise BrowserActionError(f"login_error tras {intentos} intentos — {ultimo}")

    def _login_una_vez(self):
        page = self._page
        page.goto(f"{SGS_URL}/web/login", wait_until="domcontentloaded", timeout=WAIT_TIMEOUT)

        # Autenticar la sesión del navegador por el endpoint de sesión de Odoo.
        # Deja la MISMA cookie de sesión que el formulario, pero es determinista:
        # el portal actual de SGS no envía el form de forma fiable cuando slow_mo
        # está activo (ni con clic al botón ni con Enter) y quedaba en /web/login.
        res = page.evaluate(
            """async ([db, login, password]) => {
                const r = await fetch('/web/session/authenticate', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({jsonrpc: "2.0", method: "call",
                                          params: {db: db, login: login, password: password}})
                });
                const j = await r.json();
                if (j.error) {
                    const d = j.error.data || {};
                    return {ok: false, msg: d.message || j.error.message || 'error'};
                }
                return {ok: !!(j.result && j.result.uid), uid: j.result && j.result.uid};
            }""",
            [SGS_DB, self._sgs_login, self._sgs_password],
        )
        if not res.get("ok"):
            raise BrowserActionError(
                f"login_error — SGS rechazó las credenciales: {res.get('msg')}")
        log.info(f"[LOGIN] Sesión iniciada (uid={res.get('uid')}, login={self._sgs_login})")

        # Inicializar company context (obligatorio en headless)
        page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded", timeout=WAIT_TIMEOUT)
        if "/web/login" in page.url:      # rebotó al login = sesión no activa
            raise BrowserActionError("login_error — la sesión no quedó activa en el navegador")
        self._logueado = True
        log.info("[LOGIN] OK")

    # ── Navegación double-goto (regla obligatoria para headless) ──────────────
    def _navegar(self, url_hash: str):
        """
        Double-goto: primero /web?cids=1 limpio, luego la URL real.
        Fuerza al router SPA de Odoo 17 a reiniciar correctamente.
        """
        page = self._page
        page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded", timeout=WAIT_TIMEOUT)
        if "/web/login" in page.url:
            # SGS cerró la sesión de este navegador (visto 2026-09-11 y 2026-09-23:
            # el 2º navegador quedaba en el login y cada hoja moría por nav_timeout).
            # Pre-GUARDAR: no se escribió nada → el job re-loguea y reintenta.
            raise BrowserActionError("session_expired — SGS devolvió el login (sesión cerrada)")
        self._descartar_pendiente()
        page.goto(f"{SGS_URL}{url_hash}", wait_until="domcontentloaded", timeout=WAIT_TIMEOUT)

    def _descartar_pendiente(self):
        """Descarta cualquier diálogo de 'guardar cambios' antes de navegar."""
        page = self._page
        try:
            for text in ["DESCARTAR", "Discard", "No guardar", "Don't save"]:
                btn = page.locator(f"button:has-text('{text}')")
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    time.sleep(0.3)
                    return
        except Exception:
            pass

    # ── Dismiss errores Odoo ──────────────────────────────────────────────────
    def _dismiss_error(self):
        """
        Solo actúa en .o_error_dialog o título con 'rror'/'Advertencia'.
        NUNCA toca diálogos de 'guardar cambios' — eso dispara doble escritura.
        """
        page = self._page
        try:
            dialog = page.locator(".o_error_dialog, .modal-dialog")
            if not dialog.count():
                return
            title = dialog.locator(".modal-title").inner_text() if dialog.locator(".modal-title").count() else ""
            if any(w in title for w in ["rror", "Advertencia", "Warning"]):
                for text in ["Cerrar", "Close", "OK", "Aceptar"]:
                    btn = dialog.locator(f"button:has-text('{text}')")
                    if btn.count():
                        btn.first.click()
                        return
        except Exception:
            pass

    # ── Screenshot de diagnóstico ─────────────────────────────────────────────
    def screenshot(self, nombre: str = "debug"):
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
        path  = os.path.join(log_dir, f"{nombre}_{ts}.png")
        self._page.screenshot(path=path, full_page=True)
        log.info(f"[SCREENSHOT] {path}")
        return path

    # ── Inspección de formularios desconocidos ────────────────────────────────
    def inspect_form(self, model: str, record_id=None, menu_id: int = None,
                     extra_params: str = "") -> dict:
        """
        Navega al form de un modelo y extrae toda la información disponible.
        Usa esto en Fase 1 para mapear campos y selectores antes de automatizar.

        Args:
            model:      nombre del modelo Odoo (ej: "project.project")
            record_id:  ID numérico del registro, o "new" para form de creación
            menu_id:    menu_id necesario para el contexto (buscar en URL al navegar manualmente)
            extra_params: parámetros adicionales de URL (ej: "action=xxx")

        Returns:
            dict con: url, fields, buttons, html_snippet, screenshot_path
        """
        record_str = str(record_id) if record_id else "new"
        cids_part  = "?cids=1"
        if menu_id:
            cids_part += f"&menu_id={menu_id}"
        if extra_params:
            cids_part += f"&{extra_params}"
        url_hash = f"/web{cids_part}#model={model}&id={record_str}&view_type=form"

        log.info(f"[INSPECT] Navegando a: {SGS_URL}{url_hash}")
        self._navegar(url_hash)

        page = self._page
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot(f"inspect_nav_timeout_{model.replace('.','_')}")
            raise BrowserActionError(f"nav_timeout — form no cargó. Screenshot: {shot}")

        # Extraer campos del formulario
        fields_info = page.evaluate("""() => {
            const fields = [];
            document.querySelectorAll('.o_field_widget').forEach(el => {
                const name = el.getAttribute('name') || '';
                const type = el.classList.toString();
                const label_el = document.querySelector(`label[for="${name}"], .o_wrap_label[for="${name}"]`);
                const label = label_el ? label_el.innerText.trim() : '';
                const input = el.querySelector('input, select, textarea');
                const input_type = input ? input.type || input.tagName.toLowerCase() : 'none';
                const required = el.classList.contains('o_required_modifier') ||
                                  (input && input.required);
                const value = input ? (input.value || input.innerText || '') :
                              (el.innerText || '').trim().substring(0, 80);
                fields.push({name, label, type_classes: type, input_type, required, value});
            });
            return fields;
        }""")

        # Extraer botones visibles
        buttons_info = page.evaluate("""() => {
            const btns = [];
            document.querySelectorAll('button:not([style*="display: none"]):not(.o_invisible_modifier)').forEach(btn => {
                const text = btn.innerText.trim();
                if (text) btns.push({text, classes: btn.className});
            });
            return btns;
        }""")

        # HTML del form para análisis manual
        html_snippet = page.evaluate("""() => {
            const form = document.querySelector('.o_form_view');
            return form ? form.innerHTML.substring(0, 8000) : 'FORM NO ENCONTRADO';
        }""")

        shot = self.screenshot(f"inspect_{model.replace('.','_')}")

        # Guardar reporte completo
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        reporte = {
            "url":             f"{SGS_URL}{url_hash}",
            "model":           model,
            "record_id":       record_str,
            "fields":          fields_info,
            "buttons":         buttons_info,
            "screenshot":      shot,
            "timestamp":       datetime.now().isoformat(),
        }
        reporte_path = os.path.join(log_dir, f"inspect_{model.replace('.','_')}_{record_str}.json")
        with open(reporte_path, "w", encoding="utf-8") as f:
            json.dump(reporte, f, ensure_ascii=False, indent=2)

        html_path = os.path.join(log_dir, f"inspect_{model.replace('.','_')}_{record_str}.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_snippet)

        log.info(f"[INSPECT] Reporte guardado: {reporte_path}")
        return reporte

    # ── Helpers para campos Odoo 17 ───────────────────────────────────────────

    def _fill_char(self, field_name: str, value: str):
        """Rellena un campo char/text.
        En Odoo 17 el input puede ser el propio .o_field_widget (caso nombre)
        o estar anidado dentro de él (caso campos secundarios).
        """
        page = self._page
        sel = (f"input.o_field_widget[name='{field_name}'], "
               f"textarea.o_field_widget[name='{field_name}'], "
               f".o_field_widget[name='{field_name}'] input, "
               f".o_field_widget[name='{field_name}'] textarea")
        inp = page.locator(sel).first
        inp.scroll_into_view_if_needed()
        inp.wait_for(state="visible", timeout=15_000)
        inp.click(click_count=3)
        inp.fill(value)

    def _select_autocomplete_first(self):
        """Selecciona el primer item del dropdown de autocomplete activo.

        Hay múltiples ul.ui-autocomplete en la página (uno por campo many2one).
        Solo el del campo activo tiene display:block. Buscamos el visible
        y usamos el mecanismo nativo de jQuery UI para confirmar la selección.
        """
        page = self._page
        page.wait_for_timeout(800)
        # Selector restringido al dropdown visible (display:block = activo)
        visible_sel = (
            "ul.ui-autocomplete[style*='display: block'] li.ui-menu-item:not(.ui-state-disabled), "
            "ul.ui-autocomplete[style*='display:block'] li.ui-menu-item:not(.ui-state-disabled)"
        )
        try:
            page.wait_for_selector(visible_sel, state="visible", timeout=5_000)
            page.locator(visible_sel).first.click(timeout=3_000)
        except Exception:
            # Fallback: buscar cualquier li attached y simular selección jQuery UI
            page.evaluate("""() => {
                // Encontrar el ul visible
                const ul = Array.from(document.querySelectorAll('ul.ui-autocomplete')).find(
                    el => el.style.display === 'block' || getComputedStyle(el).display === 'block'
                );
                if (!ul) return;
                const li = ul.querySelector('li.ui-menu-item:not(.ui-state-disabled)');
                if (!li) return;
                // jQuery UI requiere mouseenter para activar el ítem, luego mousedown
                $(li).trigger('mouseenter');
                $(li).trigger('mouseover');
                $(li).trigger('mousedown');
                $(li).trigger('click');
            }""")
        page.wait_for_timeout(400)

    def _fill_many2one(self, field_name: str, search_text: str):
        """Busca y selecciona la primera opción de un campo many2one."""
        page = self._page
        widget = page.locator(f".o_field_widget[name='{field_name}']").first
        widget.scroll_into_view_if_needed()
        widget.wait_for(state="visible", timeout=15_000)
        inp = widget.locator("input").first
        inp.click(click_count=3)
        inp.fill("")
        # fill() acepta Unicode exacto (incluyendo \xa0).
        # dispatch_event('input') fuerza al autocomplete de Odoo a buscar.
        inp.fill(search_text)
        inp.dispatch_event("input")
        page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=15_000
        )
        self._select_autocomplete_first()

    def _fill_many2many_tag(self, field_name: str, search_text: str):
        """Agrega un tag a un campo many2many (o_field_many2manytags)."""
        page = self._page
        widget = page.locator(f".o_field_many2manytags[name='{field_name}']").first
        widget.scroll_into_view_if_needed()
        widget.wait_for(state="visible", timeout=15_000)
        inp = widget.locator("input").first
        inp.click()
        inp.fill(search_text)
        inp.dispatch_event("input")
        page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=15_000
        )
        self._select_autocomplete_first()

    def _fill_many2one_if_empty(self, field_name: str, search_text: str):
        """Igual que _fill_many2one pero solo actúa si el campo está vacío."""
        page = self._page
        widget = page.locator(f".o_field_widget[name='{field_name}']")
        if not widget.count():
            return
        inp = widget.locator("input").first
        if inp.count() and inp.input_value():
            log.info(f"[FILL] {field_name} ya tiene valor — omitido")
            return
        self._fill_many2one(field_name, search_text)

    def _fill_date(self, field_name: str, value: str):
        """Rellena un campo date (formato MM/DD/YYYY para datepicker Odoo)."""
        page = self._page
        widget = page.locator(f".o_field_widget[name='{field_name}']")
        widget.scroll_into_view_if_needed()
        widget.wait_for(state="visible", timeout=15_000)
        inp = widget.locator("input").first
        inp.click(click_count=3)
        inp.fill(value)
        inp.press("Escape")  # cerrar datepicker si abre

    # ── Crear proyecto hijo (NODO) ────────────────────────────────────────────

    def crear_proyecto_hijo(self,
                            parent_id: int,
                            nombre: str,
                            nodo: str,
                            ticket: str,
                            pdl: str,
                            contract_id_name: str,
                            department_id_name: str,
                            technology_id_name: str,
                            formula_id_name: str,
                            approve_id_name: str,
                            customer_id_name: str,
                            region_id_name: str,
                            macro_id_name: str,
                            micro_id_name: str,
                            process_id_name: str,
                            process_type_id_name: str,
                            bandeja_approver_name: str,
                            date_assignment: str,
                            dry_run: bool = True,
                            screenshots: bool = True,
                            save_lock=None) -> dict:
        """
        Crea un proyecto hijo (NODO) en SGS replicando las acciones del usuario.

        Protocolo:
          - dry_run=True  → llega hasta el estado 'listo para guardar', NO hace click en GUARDAR.
          - dry_run=False → ejecuta el click GUARDAR real (MAX 1 intento).

        Returns: dict con resultado (code, id, screenshot_paths)
        """
        page = self._page
        shots = []

        log.info(f"[CREAR_HIJO] Iniciando — parent_id={parent_id} nombre='{nombre}' dry_run={dry_run}")

        # ── Flujo correcto — réplica exacta del flujo humano ─────────────────
        #
        # Paso 1: abrir el form del proyecto PADRE.
        # action=485 es el action del modelo project.dominion — sin él el SPA
        # carga la shell vacía. Con él, el router sabe qué vista montar.
        url_padre = (f"/web#id={parent_id}&action=485"
                     f"&model=project.dominion&view_type=form&cids=1&menu_id=352")
        self._navegar(url_padre)
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("crear_hijo_padre_timeout")
            raise BrowserActionError(f"nav_timeout — form del padre no cargó. Shot: {shot}")

        log.info(f"[CREAR_HIJO] Form padre (id={parent_id}) cargado")

        # Paso 2: click en el stat button "Proyectos" del form padre.
        # name='action_button_project' es el botón confirmado por inspección.
        # NOTA: cuando project_count=0 Odoo añade o_invisible_modifier → el botón
        # existe en el DOM pero está oculto por CSS. Usar attached + force=True.
        btn_proyectos = page.locator(
            ".o_form_view button[name='action_button_project'], "
            ".o_form_view .oe_stat_button[name='action_button_project']"
        )
        btn_proyectos.first.wait_for(state="attached", timeout=15_000)
        btn_proyectos.first.click(force=True)

        # Paso 3: esperar la vista lista de hijos (genera la URL con active_id)
        try:
            page.wait_for_selector(".o_list_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("crear_hijo_lista_timeout")
            raise BrowserActionError(f"nav_timeout — lista de hijos no cargó. Shot: {shot}")

        log.info("[CREAR_HIJO] Vista lista de hijos cargada")

        # Paso 4: click en NUEVO para abrir el form de creación con contexto heredado
        btn_nuevo = page.locator(
            "button.o_list_button_add, "
            "button:has-text('Nuevo'), "
            "button:has-text('NUEVO'), "
            "button:has-text('New')"
        )
        btn_nuevo.first.wait_for(state="visible", timeout=15_000)
        btn_nuevo.first.click()

        # Paso 5: esperar el form de creación
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("crear_hijo_form_timeout")
            raise BrowserActionError(f"nav_timeout — form de creación no cargó. Shot: {shot}")

        log.info("[CREAR_HIJO] Form de creación cargado con contexto de padre")

        if screenshots:
            shots.append(self.screenshot("crear_hijo_01_form_abierto"))

        # ══════════════════════════════════════════════════════════════════════
        # SECUENCIA CONFIRMADA POR GRABACIÓN (observar_creacion.py 2026-04-16)
        #
        # 1. Nombre
        # 2. Contrato  ← onchange MASTER: revela nodo/pdl/ticket + auto-rellena
        #    cascada completa: department, technology, formula, supervisor,
        #    circuito, gestor_operativo, pdii, process_id, process_type_id, etc.
        # 3. Departamento (si no fue auto-rellenado por el contrato)
        # 4. Nodo      ← visible SOLO después del onchange de contrato
        # 5. PDL       ← ídem
        # 6. Ticket    ← ídem
        # → GUARDAR
        # ══════════════════════════════════════════════════════════════════════

        # ── 1. Nombre ─────────────────────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 1/6 Nombre: {nombre}")
        self._fill_char("name", nombre)

        # ── 2. Contrato ───────────────────────────────────────────────────────
        # MASTER FIELD: su onchange activa require_nodo/pdl/ticket → visibilidad
        # de esos campos, y auto-rellena circuito, gestor, pdii, fórmula, etc.
        # Confirmado: nodo se vuelve visible ~3s después del click en el item.
        log.info(f"[CREAR_HIJO] 2/6 Contrato: {contract_id_name}")
        self._fill_many2one("contract_id", contract_id_name)
        # Esperar a que el onchange del contrato complete y nodo se vuelva visible
        try:
            page.wait_for_selector(
                ".o_field_widget[name='nodo']:not(.o_invisible_modifier)",
                timeout=10_000
            )
            log.info("[CREAR_HIJO] Nodo visible — onchange del contrato completado")
        except PWTimeout:
            shot = self.screenshot("crear_hijo_nodo_invisible")
            raise BrowserActionError(
                f"field_timeout — nodo sigue invisible después del onchange del contrato. Shot: {shot}"
            )

        if screenshots:
            shots.append(self.screenshot("crear_hijo_02_post_contrato"))

        # ── 3. Departamento ───────────────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 3/6 Departamento: {department_id_name}")
        self._fill_many2one_if_empty("department_id", department_id_name)

        # ── 3b. Tecnología (required — puede no auto-rellenarse) ──────────────
        # El onchange del contrato debería setearla, pero si quedó vacía la llenamos.
        log.info(f"[CREAR_HIJO] 3b. Tecnología: {technology_id_name}")
        self._fill_many2one_if_empty("technology_id", technology_id_name)

        # ── 4. Aprobadores de Bandejas ────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 4/6 Aprobadores: {bandeja_approver_name}")
        self._fill_many2many_tag("bandeja_approver_ids", bandeja_approver_name)

        # ── 5. Nodo ───────────────────────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 5/6 Nodo: {nodo}")
        self._fill_char("nodo", nodo)

        # ── 6. PDL ────────────────────────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 6/6 PDL: {pdl}")
        self._fill_char("pdl", pdl)

        # ── 7. Ticket ─────────────────────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 7/6 Ticket: {ticket}")
        self._fill_char("ticket", ticket)

        if screenshots:
            shots.append(self.screenshot("crear_hijo_02_pre_guardar"))

        log.info(f"[CREAR_HIJO] Formulario completo — dry_run={dry_run}")

        if dry_run:
            log.info("[CREAR_HIJO] DRY RUN — NO se hace click en GUARDAR. Verificar screenshot.")
            return {"status": "dry_run_ok", "screenshots": shots}

        # ── GUARDAR (solo Fase 3) — Opción D ─────────────────────────────────
        # Interceptar la respuesta HTTP del create para capturar el nuevo ID
        # directamente del JSON de Odoo. Más confiable que la URL hash,
        # especialmente con múltiples workers en paralelo.
        # Regla «sin reintento tras guardar»: después de este click, NO reintentar aunque haya timeout.
        log.info("[CREAR_HIJO] Ejecutando GUARDAR...")

        save_btn = page.locator("button.o_form_button_save, button:has-text('Guardar')")
        save_btn.wait_for(state="visible", timeout=10_000)

        def _es_create_dominion(response):
            try:
                if "call_kw" not in response.url:
                    return False
                body = response.request.post_data or ""
                return '"create"' in body and '"project.dominion"' in body
            except Exception:
                return False

        nuevo_id = None
        _lock_acquired = False
        try:
            if save_lock is not None:
                save_lock.acquire()
                _lock_acquired = True
            try:
                with page.expect_response(_es_create_dominion, timeout=30_000) as resp_info:
                    save_btn.first.click()
                resp_data = resp_info.value.json()
            finally:
                if _lock_acquired:
                    save_lock.release()
                    _lock_acquired = False
            if resp_data.get("error"):
                raise BrowserActionError(f"save_error — {resp_data['error']}")
            result = resp_data.get("result")
            if isinstance(result, int):
                nuevo_id = result
        except PWTimeout:
            if _lock_acquired:
                save_lock.release()
            # El click ya se ejecutó: no reintentar (regla «sin reintento tras guardar»). ID desconocido.
            log.warning("[CREAR_HIJO] save_timeout — intercepción no capturó respuesta")
            raise BrowserActionError("save_timeout — GUARDAR ejecutado pero sin respuesta capturada")

        if screenshots:
            shots.append(self.screenshot("crear_hijo_03_post_guardar"))

        # Verificar si apareció error de Odoo en UI (validación client-side)
        self._dismiss_error()
        error_dlg = page.locator(".o_error_dialog, .modal-dialog .modal-title")
        if error_dlg.count():
            txt = error_dlg.first.inner_text()
            raise BrowserActionError(f"save_error — {txt}")

        log.info(f"[CREAR_HIJO] CREADO OK — id={nuevo_id}")
        return {"status": "ok", "id": nuevo_id, "screenshots": shots}

    def crear_bandeja(self, nodo_id: int, partner_name: str,
                      date_str: str = "30/05/2026", location_search: str = "UBAT2/Stock",
                      dry_run: bool = True, screenshots: bool = True) -> dict:
        """
        Crea una bandeja de despacho para un NODO.

        Flow: form NODO → Materiales → select-all → CREAR BANDEJA → modal
              → Equipo + Fecha + Almacén → GUARDAR → BAN code → APROBAR.

        Campos del modal (mapeados 2026-05-13):
          partner_id  = Equipo (many2one)
          date        = Fecha
          location_id = Almacén (many2one, buscar con "UBAT2")
          GUARDAR     = button sin name, scope modal
          APROBAR     = button[name='approve']

        Returns: {status, ban_code, estado, screenshots}
        """
        page = self._page
        shots = []

        # ── Paso 1: Form del NODO ─────────────────────────────────────────────
        url_nodo = (f"/web#id={nodo_id}&action=485"
                    f"&model=project.dominion&view_type=form&cids=1&menu_id=352")
        self._navegar(url_nodo)
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("bandeja_nodo_timeout")
            raise BrowserActionError(f"nav_timeout — form NODO no cargó. Shot: {shot}")

        log.info(f"[BANDEJA] Form NODO id={nodo_id} cargado")

        # ── Paso 2: Click Materiales ──────────────────────────────────────────
        btn_mat = page.locator("button[name='action_button_material']")
        btn_mat.first.wait_for(state="visible", timeout=15_000)
        btn_mat.first.click()
        try:
            page.wait_for_selector(".o_list_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("bandeja_materiales_timeout")
            raise BrowserActionError(f"nav_timeout — lista materiales no cargó. Shot: {shot}")

        # Esperar a que al menos una fila sea visible antes de contar
        try:
            page.wait_for_selector(".o_data_row", timeout=40_000)
        except PWTimeout:
            shot = self.screenshot("bandeja_sin_materiales")
            raise BrowserActionError(f"dropdown_timeout — NODO sin materiales en lista. Shot: {shot}")
        page.wait_for_timeout(500)
        n_filas = page.locator(".o_data_row").count()

        log.info(f"[BANDEJA] {n_filas} materiales en lista")

        # ── Paso 3: Select-all via JS ─────────────────────────────────────────
        clicked = page.evaluate("""() => {
            const cb = document.querySelector(
                'th.o_list_record_selector input[type="checkbox"]');
            if (cb) { cb.click(); return true; }
            return false;
        }""")
        if not clicked:
            raise BrowserActionError("field_timeout — checkbox select-all no encontrado")

        page.wait_for_timeout(800)
        log.info("[BANDEJA] Select-all ejecutado")

        if screenshots:
            shots.append(self.screenshot("bandeja_01_seleccionados"))

        # ── Paso 4: Click CREAR BANDEJA ───────────────────────────────────────
        btn_tray = page.locator("button[name='action_button_create_tray']")
        btn_tray.first.wait_for(state="visible", timeout=25_000)
        btn_tray.first.click()

        page.wait_for_timeout(3000)
        try:
            page.wait_for_selector(".modal.show", timeout=30_000)
        except PWTimeout:
            shot = self.screenshot("bandeja_modal_timeout")
            raise BrowserActionError(f"field_timeout — modal CREAR BANDEJA no abrió. Shot: {shot}")

        modal = page.locator(".modal.show").first
        log.info("[BANDEJA] Modal abierto")

        # ── Detectar Error de Validación antes de esperar campos ──────────────
        # Odoo puede abrir un modal de error en lugar del form de creación.
        # "No hay materiales que puedan ser pedidos por bandeja" = balance 0.
        page.wait_for_timeout(500)
        modal_body = page.locator(".modal.show .modal-body")
        if modal_body.count() > 0 and modal_body.first.is_visible():
            try:
                txt = modal_body.first.inner_text(timeout=2_000)
            except Exception:
                txt = ""
            if "No hay materiales" in txt or "materiales que puedan" in txt:
                aceptar = page.locator(
                    ".modal.show button:has-text('ACEPTAR'), "
                    ".modal.show button:has-text('Aceptar')"
                )
                if aceptar.count() > 0:
                    aceptar.first.click()
                    page.wait_for_timeout(500)
                raise BrowserActionError(
                    "sin_materiales — Odoo: No hay materiales disponibles para esta bandeja"
                )

        # Esperar que los campos del form dentro del modal estén en el DOM
        try:
            page.wait_for_selector(
                ".modal.show .o_field_widget[name='partner_id']",
                state="visible", timeout=45_000,
            )
        except PWTimeout:
            shot = self.screenshot("bandeja_modal_campos_timeout")
            raise BrowserActionError(f"field_timeout — campos del modal no renderizaron. Shot: {shot}")

        if screenshots:
            shots.append(self.screenshot("bandeja_02_modal"))

        # ── Paso 5: Rellenar Equipo (partner_id) ──────────────────────────────
        log.info(f"[BANDEJA] Equipo: {partner_name}")
        p_widget = modal.locator(".o_field_widget[name='partner_id']").first
        p_widget.wait_for(state="visible", timeout=10_000)
        p_inp = p_widget.locator("input").first
        p_inp.click(click_count=3)
        p_inp.fill(partner_name)
        p_inp.dispatch_event("input")
        page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=25_000,
        )
        self._select_autocomplete_first()

        # ── Paso 6: Rellenar Fecha (date) ─────────────────────────────────────
        # Odoo 17: fill() no dispara eventos del datepicker. Usar press_sequentially
        # para que cada tecla actualice el picker, luego Enter para confirmar.
        # Escape revierte el valor → NO usar Escape.
        log.info(f"[BANDEJA] Fecha: {date_str}")
        d_widget = modal.locator(".o_field_widget[name='date']").first
        d_widget.wait_for(state="visible", timeout=10_000)
        d_inp = d_widget.locator("input").first
        d_inp.click(click_count=3)
        d_inp.press_sequentially(date_str)
        d_inp.press("Enter")
        page.wait_for_timeout(500)

        # ── Paso 7: Rellenar Almacén (location_id) ────────────────────────────
        log.info(f"[BANDEJA] Almacén: {location_search}")
        l_widget = modal.locator(".o_field_widget[name='location_id']").first
        l_widget.wait_for(state="visible", timeout=10_000)
        l_inp = l_widget.locator("input").first
        l_inp.click(click_count=3)
        l_inp.fill(location_search)
        l_inp.dispatch_event("input")
        page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=25_000,
        )
        self._select_autocomplete_first()

        if screenshots:
            shots.append(self.screenshot("bandeja_03_pre_guardar"))

        log.info(f"[BANDEJA] Modal listo — dry_run={dry_run}")

        if dry_run:
            log.info("[BANDEJA] DRY RUN — NO se guarda")
            return {"status": "dry_run_ok", "ban_code": None, "estado": None,
                    "screenshots": shots}

        # ── Paso 8: GUARDAR ───────────────────────────────────────────────────
        # Regla «sin reintento tras guardar»: después de este click, NO reintentar.
        log.info("[BANDEJA] Ejecutando GUARDAR...")
        save_btn = modal.locator("button:has-text('GUARDAR')")
        save_btn.first.wait_for(state="visible", timeout=10_000)
        save_btn.first.click()

        # Esperar que el nombre cambie de "New" a BAN-XXXXXX
        try:
            page.wait_for_function("""() => {
                const modal = document.querySelector('.modal.show');
                if (!modal) return true;
                const f = modal.querySelector('.o_field_widget[name="name"]');
                if (!f) return true;
                const text = f.innerText.trim();
                return text && text !== 'New' && !text.startsWith('New');
            }""", timeout=45_000)
        except PWTimeout:
            pass  # sin reintento tras guardar, aunque haya timeout

        page.wait_for_timeout(1000)
        self._dismiss_error()

        if screenshots:
            shots.append(self.screenshot("bandeja_04_post_guardar"))

        # Extraer BAN code del campo name del modal
        ban_code = page.evaluate("""() => {
            const modal = document.querySelector('.modal.show');
            if (!modal) return '';
            const f = modal.querySelector('.o_field_widget[name="name"]');
            return f ? f.innerText.trim() : '';
        }""")
        if not ban_code or ban_code == "New":
            ban_code = None
            log.warning("[BANDEJA] BAN code no detectado — verificar en SGS")
        else:
            log.info(f"[BANDEJA] BAN code: {ban_code}")

        # ── Paso 9: APROBAR ───────────────────────────────────────────────────
        log.info("[BANDEJA] Ejecutando APROBAR...")
        try:
            aprobar = modal.locator("button[name='approve']").first
            aprobar.wait_for(state="visible", timeout=10_000)
            aprobar.click()
            page.wait_for_timeout(3000)
        except Exception as e:
            log.warning(f"[BANDEJA] APROBAR: {e}")

        if screenshots:
            shots.append(self.screenshot("bandeja_05_aprobado"))

        # Leer estado: si el modal se cerró tras APROBAR = APROBADO
        modal_abierto = page.locator(".modal.show").count() > 0
        if not modal_abierto:
            estado = "APROBADO"
        else:
            estado = page.evaluate("""() => {
                const modal = document.querySelector('.modal.show');
                if (!modal) return 'APROBADO';
                const cur = modal.querySelector(
                    '.o_statusbar_status button.o_arrow_button_current');
                return cur ? cur.innerText.trim() : '';
            }""")
        log.info(f"[BANDEJA] Estado: {estado}")

        return {"status": "ok", "ban_code": ban_code, "estado": estado,
                "screenshots": shots}

    # ══════════════════════════════════════════════════════════════════════
    #  BANDEJA DE DEVOLUCIÓN (mapeado 2026-08-03 con explorar_bandeja_devolucion.py)
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _fmt_qty(q) -> str:
        """Cantidad en formato Odoo colombiano (coma decimal)."""
        q = float(q)
        if q == int(q):
            return str(int(q))
        return str(q).replace(".", ",")

    @staticmethod
    def _num_celda(txt) -> float:
        """Número de una celda de Odoo en formato colombiano: '7 292,00',
        '7.292,00', '79,00'. El separador de miles puede ser un espacio
        (incluido el NBSP/espacio fino de Odoo): con '7 292,00' el parseo viejo
        fallaba y el Proyectado de 310413 (ya guardado bien) salió como
        save_error (incidente 2026-10-05, BAN-127729)."""
        s = re.sub(r"\s", "", str(txt))
        return float(s.replace(".", "").replace(",", "."))

    def crear_bandeja_devolucion(self, nodo_id: int, partner_name: str,
                                 materiales: list, date_str: str,
                                 location_search: str = "UBAT2/Recupero",
                                 dry_run: bool = True,
                                 screenshots: bool = True,
                                 aprobar_si=None) -> dict:
        """
        Crea una bandeja de DEVOLUCIÓN para un NODO seleccionando SOLO los
        materiales indicados y ajustando la cantidad a devolver de cada uno.

        materiales: [{"ref": "330015", "dom": "DOM-037082", "cantidad": 4}, ...]
                    ref = referencia interna (tras la barra en [DOM-XXX/REF]).

        Flujo (réplica del flujo manual del operador):
          form NODO → Materiales → seleccionar filas de los materiales
          → CREAR BANDEJA DE DEVOLUCIÓN (button[name='action_button_create_devolution_tray'])
          → modal: ajustar cantidad por material + Equipo + Fecha + Almacén
          → GUARDAR. NO se pulsa APROBAR: la aprobación es de almacén al recibir.

        Si Odoo responde "No se puede devolver ninguno de los materiales..."
        (sin saldo pendiente) → BrowserActionError('sin_saldo — ...').

        Returns: {status, ban_code, estado, advertencias, screenshots}
        """
        page = self._page
        shots = []
        advertencias = []

        # ── Paso 1: Form del NODO → Materiales ────────────────────────────────
        url_nodo = (f"/web#id={nodo_id}&action=485"
                    f"&model=project.dominion&view_type=form&cids=1&menu_id=352")
        self._navegar(url_nodo)
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("dev_nodo_timeout")
            raise BrowserActionError(f"nav_timeout — form NODO no cargó. Shot: {shot}")

        page.locator("button[name='action_button_material']").first.click()
        try:
            page.wait_for_selector(".o_data_row", timeout=45_000)
        except PWTimeout:
            shot = self.screenshot("dev_materiales_timeout")
            raise BrowserActionError(f"nav_timeout — lista materiales no cargó. Shot: {shot}")
        page.wait_for_timeout(700)
        log.info(f"[DEVOLUCION] NODO {nodo_id}: lista de materiales cargada")

        # ── Paso 2: Seleccionar SOLO las filas de los materiales a devolver ──
        #    (click JS: el label de Odoo tapa el checkbox)
        seleccionados = []
        for mat in materiales:
            ref = str(mat.get("ref") or "").strip()
            dom = str(mat.get("dom") or "").strip()
            ok = page.evaluate("""(sel) => {
                const filas=[...document.querySelectorAll('.o_data_row')];
                const fila=filas.find(r =>
                    (sel.ref && r.textContent.includes('/'+sel.ref+']')) ||
                    (sel.dom && r.textContent.includes(sel.dom)));
                if(!fila) return false;
                const cb=fila.querySelector('.o_list_record_selector input');
                if(cb && !cb.checked){ cb.click(); }
                return !!cb;
            }""", {"ref": ref, "dom": dom})
            if ok:
                seleccionados.append(mat)
            else:
                advertencias.append(f"Material {dom}/{ref} no encontrado en la lista — omitido")
                log.warning(f"[DEVOLUCION] fila no encontrada: {dom}/{ref}")
            page.wait_for_timeout(250)

        if not seleccionados:
            raise BrowserActionError("sin_materiales — ninguna fila coincidió en la lista")
        page.wait_for_timeout(800)
        log.info(f"[DEVOLUCION] {len(seleccionados)}/{len(materiales)} filas seleccionadas")
        if screenshots:
            shots.append(self.screenshot("dev_01_seleccion"))

        # ── Paso 3: CREAR BANDEJA DE DEVOLUCIÓN ──────────────────────────────
        btn = page.locator("button[name='action_button_create_devolution_tray']")
        btn.first.wait_for(state="visible", timeout=25_000)
        btn.first.click()

        page.wait_for_timeout(2500)
        try:
            page.wait_for_selector(".modal.show", timeout=30_000)
        except PWTimeout:
            shot = self.screenshot("dev_modal_timeout")
            raise BrowserActionError(f"field_timeout — modal devolución no abrió. Shot: {shot}")

        modal = page.locator(".modal.show").first
        page.wait_for_timeout(800)

        # ── Detección del error de validación de Odoo ─────────────────────────
        cuerpo = ""
        try:
            cuerpo = modal.inner_text(timeout=3_000)
        except Exception:
            pass
        if "No se puede devolver" in cuerpo or "Error de Validación" in cuerpo:
            if screenshots:
                shots.append(self.screenshot("dev_error_validacion"))
            aceptar = page.locator(".modal.show button:has-text('ACEPTAR'), "
                                   ".modal.show button:has-text('Aceptar')")
            if aceptar.count() > 0:
                aceptar.first.click()
                page.wait_for_timeout(500)
            raise BrowserActionError(
                "sin_saldo — Odoo: los materiales seleccionados no tienen saldo "
                "pendiente de devolución (¿ya se devolvieron?)")

        try:
            page.wait_for_selector(".modal.show .o_field_widget[name='partner_id']",
                                   state="visible", timeout=45_000)
        except PWTimeout:
            shot = self.screenshot("dev_modal_campos_timeout")
            raise BrowserActionError(f"field_timeout — campos del modal no renderizaron. Shot: {shot}")
        log.info("[DEVOLUCION] Modal de devolución abierto")

        # ── Paso 4: Ajustar la CANTIDAD A DEVOLVER de cada material ──────────
        # La celda numérica (3ª) es editable inline: click → input → escribir →
        # blur clicando el título del modal (Escape revierte, NO usarlo).
        # El match de fila busca en textContent Y en los title de las celdas
        # (el texto visible puede venir truncado con puntos suspensivos).
        o2m = modal.locator(".o_field_widget[name='project_tray_material_ids']")
        sin_ajustar = []
        for mat in seleccionados:
            ref = str(mat.get("ref") or "").strip()
            dom = str(mat.get("dom") or "").strip()
            qty = self._fmt_qty(mat["cantidad"])
            idx = page.evaluate("""(sel) => {
                const cont = document.querySelector(
                    ".modal.show .o_field_widget[name='project_tray_material_ids']");
                if (!cont) return -1;
                const filas = [...cont.querySelectorAll('.o_data_row')];
                return filas.findIndex(r => {
                    const t = (r.textContent || '') + ' ' +
                        [...r.querySelectorAll('td')]
                          .map(td => td.getAttribute('title') || '').join(' ');
                    return (sel.ref && t.includes('/' + sel.ref + ']')) ||
                           (sel.dom && t.includes(sel.dom));
                });
            }""", {"ref": ref, "dom": dom})
            if idx < 0:
                # Odoo excluyó el material de la bandeja (p.ej. picking pendiente):
                # NO se guarda nada de él → no hay riesgo de devolver de más.
                advertencias.append(
                    f"⚠ {dom}/{ref}: Odoo NO lo incluyó en la bandeja — queda SIN "
                    f"devolver, hacerlo a mano o en otra bandeja")
                log.warning(f"[DEVOLUCION] {dom}/{ref} excluido por Odoo del modal")
                continue
            fila = o2m.locator(".o_data_row").nth(idx)
            celda = fila.locator("td.o_list_number").first
            celda.click()
            try:
                inp = fila.locator("input").first
                inp.wait_for(state="visible", timeout=8_000)
            except PWTimeout:
                # la fila editada puede re-renderizarse como .o_selected_row
                inp = modal.locator(".o_selected_row input").first
                inp.wait_for(state="visible", timeout=8_000)
            inp.click(click_count=3)
            inp.press_sequentially(qty)
            # blur para confirmar la fila (nunca Escape)
            modal.locator(".modal-title, h4").first.click()
            page.wait_for_timeout(600)
            # Confirmar que la celda quedó con la cantidad pedida
            try:
                celda_txt = (o2m.locator(".o_data_row").nth(idx)
                             .locator("td.o_list_number").first.inner_text(timeout=3_000))
                val = self._num_celda(celda_txt)
                if abs(val - float(mat["cantidad"])) > 0.001:
                    sin_ajustar.append(f"{dom}/{ref}")
                    advertencias.append(
                        f"{dom}/{ref}: la celda quedó en {celda_txt.strip()} y no en {qty}")
                    log.warning(f"[DEVOLUCION] {dom}/{ref} celda={celda_txt} != {qty}")
                    continue
            except Exception:
                pass
            log.info(f"[DEVOLUCION] {dom}/{ref} → cantidad {qty}")

        # SEGURIDAD: en modo REAL, si un material SÍ está en el modal pero su
        # cantidad no se pudo ajustar/confirmar, guardar dejaría la cantidad
        # prellenada (saldo completo) → devolución de más. Se descarta la
        # bandeja entera y se reporta como error.
        if sin_ajustar and not dry_run:
            if screenshots:
                shots.append(self.screenshot("dev_abort_ajuste"))
            descartar = modal.locator("button:has-text('Descartar'), "
                                      "button:has-text('DESCARTAR')")
            if descartar.count() > 0:
                descartar.first.click()
                page.wait_for_timeout(800)
            raise BrowserActionError(
                "ajuste_cantidad — " + ", ".join(sin_ajustar) +
                " no se pudieron ajustar en el modal; bandeja DESCARTADA sin guardar "
                "(evita devolver de más). Revisar ese material a mano.")

        if screenshots:
            shots.append(self.screenshot("dev_02_cantidades"))

        # ── Paso 5: Equipo (partner_id) ──────────────────────────────────────
        log.info(f"[DEVOLUCION] Equipo: {partner_name}")
        p_widget = modal.locator(".o_field_widget[name='partner_id']").first
        p_inp = p_widget.locator("input").first
        p_inp.click(click_count=3)
        p_inp.fill(partner_name)
        p_inp.dispatch_event("input")
        page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=25_000)
        self._select_autocomplete_first()

        # ── Paso 6: Fecha ────────────────────────────────────────────────────
        log.info(f"[DEVOLUCION] Fecha: {date_str}")
        d_inp = modal.locator(".o_field_widget[name='date'] input").first
        d_inp.click(click_count=3)
        d_inp.press_sequentially(date_str)
        d_inp.press("Enter")
        page.wait_for_timeout(500)

        # ── Paso 7: Almacén (location_id) → UBAT2/Recupero ───────────────────
        log.info(f"[DEVOLUCION] Almacén: {location_search}")
        l_inp = modal.locator(".o_field_widget[name='location_id'] input").first
        l_inp.click(click_count=3)
        l_inp.fill(location_search)
        l_inp.dispatch_event("input")
        page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=25_000)
        self._select_autocomplete_first()

        if screenshots:
            shots.append(self.screenshot("dev_03_pre_guardar"))
        log.info(f"[DEVOLUCION] Modal listo — dry_run={dry_run}")

        if dry_run:
            # Salir sin guardar
            descartar = modal.locator("button:has-text('Descartar'), "
                                      "button:has-text('DESCARTAR')")
            if descartar.count() > 0:
                descartar.first.click()
                page.wait_for_timeout(800)
            log.info("[DEVOLUCION] DRY RUN — modal descartado, nada guardado")
            return {"status": "dry_run_ok", "ban_code": None, "estado": None,
                    "advertencias": advertencias, "screenshots": shots}

        # ── Paso 8: GUARDAR (después de este click, NUNCA reintentar) ──────────
        log.info("[DEVOLUCION] Ejecutando GUARDAR...")
        save_btn = modal.locator("button.o_form_button_save, "
                                 "button:has-text('GUARDAR'), button:has-text('Guardar')")
        save_btn.first.wait_for(state="visible", timeout=10_000)
        save_btn.first.click()

        try:
            page.wait_for_function("""() => {
                const modal = document.querySelector('.modal.show');
                if (!modal) return true;
                const f = modal.querySelector('.o_field_widget[name="name"]');
                if (!f) return true;
                const text = f.innerText.trim();
                return text && text !== 'New' && !text.startsWith('New');
            }""", timeout=45_000)
        except PWTimeout:
            pass  # sin reintento tras guardar, aunque haya timeout

        page.wait_for_timeout(1000)
        self._dismiss_error()

        ban_code = page.evaluate("""() => {
            const modal = document.querySelector('.modal.show');
            if (!modal) return '';
            const f = modal.querySelector('.o_field_widget[name="name"]');
            return f ? f.innerText.trim() : '';
        }""")
        if not ban_code or ban_code == "New":
            ban_code = None
            log.warning("[DEVOLUCION] BAN code no detectado — VERIFICAR EN SGS antes de reintentar")
        else:
            log.info(f"[DEVOLUCION] Bandeja de devolución creada: {ban_code}")

        estado = page.evaluate("""() => {
            const modal = document.querySelector('.modal.show');
            if (!modal) return '';
            const cur = modal.querySelector('.o_statusbar_status button.o_arrow_button_current')
                     || modal.querySelector('.o_statusbar_status .o_arrow_button.btn-primary');
            return cur ? cur.innerText.trim() : '';
        }""")

        if screenshots:
            shots.append(self.screenshot("dev_04_guardada"))

        # ── APROBAR automático si la verificación externa lo autoriza ─────────
        # aprobar_si: callable(ban_code) -> bool. Compara la bandeja recién
        # guardada (lectura SGS) contra el plan; solo si TODO cuadra se aprueba.
        aprobada = False
        if ban_code and aprobar_si is not None:
            try:
                autorizado = bool(aprobar_si(ban_code))
            except Exception as e:
                autorizado = False
                advertencias.append(f"Verificación pre-APROBAR falló: {e}")
            if autorizado:
                log.info(f"[DEVOLUCION] {ban_code}: verificación OK → APROBAR")
                try:
                    ap = modal.locator("button[name='approve']").first
                    ap.wait_for(state="visible", timeout=10_000)
                    ap.click()
                    page.wait_for_timeout(3000)
                    self._dismiss_error()
                    if page.locator(".modal.show").count() == 0:
                        estado = "APROBADO"
                    else:
                        estado = page.evaluate("""() => {
                            const modal = document.querySelector('.modal.show');
                            if (!modal) return 'APROBADO';
                            const cur = modal.querySelector(
                                '.o_statusbar_status button.o_arrow_button_current');
                            return cur ? cur.innerText.trim() : '';
                        }""") or estado
                    aprobada = "APROBAD" in (estado or "").upper()
                except Exception as e:
                    advertencias.append(f"Click APROBAR falló (queda Para aprobar): {e}")
                if screenshots:
                    shots.append(self.screenshot("dev_05_aprobada"))
            else:
                advertencias.append(
                    "Verificación pre-APROBAR NO cuadró — la bandeja queda 'Para aprobar'")

        # Cerrar el modal para dejar la sesión limpia.
        try:
            cerrar = page.locator(".modal.show button.close")
            if cerrar.count() > 0:
                cerrar.first.click()
                page.wait_for_timeout(800)
        except Exception:
            pass

        return {"status": "ok", "ban_code": ban_code, "estado": estado or "Para aprobar",
                "aprobada": aprobada, "advertencias": advertencias, "screenshots": shots}

    def crear_despacho_adicional(self, nodo_id: int, items: list, partner_name: str,
                                 date_str: str, location_search: str = "UBAT2/Stock",
                                 dry_run: bool = True, screenshots: bool = True,
                                 aprobar_si=None, verificar_amounts=None) -> dict:
        """
        Despacho ADICIONAL (pedido en notas de esquina de la hoja):
          1. Sube el Proyectado de cada material a proyectado_nuevo
             (celda editable inline: input[name='amount']; commit clicando el thead).
          2. Selecciona esas filas → CREAR BANDEJA (despacho normal).
          3. Modal: Equipo + Fecha + Almacén (UBAT2/Stock) + ajustar la Cantidad
             de cada línea a la cantidad SOLICITADA (no el residuo de SGS).
          4. GUARDAR y, si aprobar_si(ban) lo autoriza, APROBAR.

        items: [{"ref": "274298", "cantidad": 3, "proyectado_nuevo": 5}]

        dry_run: NO toca el Proyectado (es una escritura) — simula selección y
        modal y descarta. Sin subir el Proyectado, Odoo puede responder que no
        hay pendiente: se reporta como advertencia esperada del test.
        """
        page = self._page
        shots = []
        advertencias = []
        proyectados = []

        url_nodo = (f"/web#id={nodo_id}&action=485"
                    f"&model=project.dominion&view_type=form&cids=1&menu_id=352")
        self._navegar(url_nodo)
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("adic_nodo_timeout")
            raise BrowserActionError(f"nav_timeout — form NODO no cargó. Shot: {shot}")
        page.locator("button[name='action_button_material']").first.click()
        try:
            page.wait_for_selector(".o_data_row", timeout=45_000)
        except PWTimeout:
            shot = self.screenshot("adic_materiales_timeout")
            raise BrowserActionError(f"nav_timeout — lista materiales no cargó. Shot: {shot}")
        page.wait_for_timeout(800)

        idx_proj = page.evaluate("""() => {
            const ths=[...document.querySelectorAll('.o_list_view thead th')];
            return ths.findIndex(th => (th.textContent||'').trim().toUpperCase()
                                        .startsWith('PROYECTADO'));
        }""")
        if idx_proj < 0:
            raise BrowserActionError("field_timeout — columna Proyectado no encontrada")

        def _idx_fila(ref):
            return page.evaluate("""(ref) => {
                const filas=[...document.querySelectorAll('.o_list_view .o_data_row')];
                return filas.findIndex(r => {
                    const t=(r.textContent||'') + ' ' + [...r.querySelectorAll('td')]
                        .map(td => td.getAttribute('title')||'').join(' ');
                    return t.includes('/' + ref + ']');
                });
            }""", ref)

        # ── Paso 1: subir el Proyectado (SOLO en modo real: es escritura) ─────
        def _editar_proyectado(ref, objetivo):
            """Edita la celda Proyectado de la fila de `ref` al valor absoluto
            `objetivo` y confirma la fila. Lanza BrowserActionError si falla."""
            nuevo = self._fmt_qty(objetivo)
            fi = _idx_fila(ref)
            if fi < 0:
                raise BrowserActionError(
                    f"sin_materiales — la referencia {ref} no está en la lista del NODO")
            fila = page.locator(".o_list_view .o_data_row").nth(fi)
            fila.locator("td").nth(idx_proj).click()
            inp = page.locator(".o_selected_row input[name='amount']").first
            try:
                inp.wait_for(state="visible", timeout=10_000)
            except PWTimeout:
                raise BrowserActionError(
                    f"field_timeout — celda Proyectado de {ref} no entró en edición")
            inp.click(click_count=3)
            inp.press_sequentially(nuevo)
            # Confirmar: click en zona neutra y esperar a que la fila salga de edición
            page.locator(".o_control_panel").first.click()
            confirmada = True
            try:
                page.wait_for_function(
                    "() => !document.querySelector('.o_list_view .o_selected_row')",
                    timeout=10_000)
            except PWTimeout:
                confirmada = False
            if not confirmada:
                filas = page.locator(".o_list_view .o_data_row")
                if filas.count() > 1:
                    filas.nth(1 if fi == 0 else 0).locator("td").nth(2).click()
                    try:
                        page.wait_for_function(
                            "() => !document.querySelector('.o_list_view .o_selected_row')",
                            timeout=8_000)
                        confirmada = True
                    except PWTimeout:
                        pass
            page.wait_for_timeout(1500)
            txt = page.locator(".o_list_view .o_data_row").nth(fi) \
                      .locator("td").nth(idx_proj).inner_text().strip()
            try:
                val = self._num_celda(txt)
            except ValueError:
                val = None
            if not confirmada or val is None or abs(val - float(objetivo)) > 0.001:
                raise BrowserActionError(
                    f"save_error — Proyectado de {ref} quedó en '{txt}' y se esperaba "
                    f"{nuevo}. Revisar el material en SGS antes de reintentar.")
            log.info(f"[ADICIONAL] {ref}: Proyectado → {nuevo} (celda confirmada)")

        if dry_run:
            for it in items:
                proyectados.append({"ref": str(it["ref"]).strip(),
                                    "nuevo": it["proyectado_nuevo"], "simulado": True})
        else:
            for it in items:
                _editar_proyectado(str(it["ref"]).strip(), it["proyectado_nuevo"])
                proyectados.append({"ref": str(it["ref"]).strip(),
                                    "nuevo": it["proyectado_nuevo"]})
            # La UI puede mostrar el valor sin haberlo persistido (visto en
            # producción): verificar por API qué quedó de verdad en SGS y
            # re-editar lo que falte, hasta 3 rondas.
            if verificar_amounts is not None:
                esperados = {str(it["ref"]).strip(): float(it["proyectado_nuevo"])
                             for it in items}
                mal = {}
                for ronda in range(3):
                    page.wait_for_timeout(1500)
                    try:
                        mal = verificar_amounts(esperados) or {}
                    except Exception as e:
                        advertencias.append(f"Verificación API de proyectados falló: {e}")
                        mal = {}
                        break
                    if not mal:
                        break
                    log.warning(f"[ADICIONAL] Proyectados sin persistir (ronda {ronda+1}): {mal}")
                    for ref in mal:
                        _editar_proyectado(ref, esperados[ref])
                if mal:
                    raise BrowserActionError(
                        "save_error — Proyectados que NO persistieron en SGS tras "
                        f"3 rondas: {mal}. Revisar a mano antes de reintentar.")
        if screenshots:
            shots.append(self.screenshot("adic_01_proyectados"))

        # ── Paso 2: seleccionar las filas ─────────────────────────────────────
        # Primero limpiar CUALQUIER selección previa (un select-all accidental
        # metería todos los materiales a la bandeja)
        page.evaluate("""() => {
            document.querySelectorAll(
                '.o_list_view .o_data_row .o_list_record_selector input:checked')
                .forEach(cb => cb.click());
        }""")
        page.wait_for_timeout(400)
        for it in items:
            ref = str(it["ref"]).strip()
            page.evaluate("""(ref) => {
                const filas=[...document.querySelectorAll('.o_list_view .o_data_row')];
                const fila=filas.find(r => (r.textContent||'').includes('/'+ref+']') ||
                    [...r.querySelectorAll('td')].some(td=>(td.getAttribute('title')||'')
                        .includes('/'+ref+']')));
                if(fila){ const cb=fila.querySelector('.o_list_record_selector input');
                          if(cb && !cb.checked) cb.click(); }
            }""", ref)
            page.wait_for_timeout(250)
        page.wait_for_timeout(800)

        # ── Paso 3: CREAR BANDEJA (despacho) ──────────────────────────────────
        btn = page.locator("button[name='action_button_create_tray']")
        btn.first.wait_for(state="visible", timeout=25_000)
        btn.first.click()
        page.wait_for_timeout(2500)
        try:
            page.wait_for_selector(".modal.show", timeout=30_000)
        except PWTimeout:
            shot = self.screenshot("adic_modal_timeout")
            raise BrowserActionError(f"field_timeout — modal no abrió. Shot: {shot}")
        modal = page.locator(".modal.show").first
        page.wait_for_timeout(800)

        cuerpo = ""
        try:
            cuerpo = modal.inner_text(timeout=3_000)
        except Exception:
            pass
        if "No hay materiales" in cuerpo or "Error de Validación" in cuerpo:
            aceptar = page.locator(".modal.show button:has-text('ACEPTAR'), "
                                   ".modal.show button:has-text('Aceptar')")
            if aceptar.count() > 0:
                aceptar.first.click()
                page.wait_for_timeout(500)
            if dry_run:
                advertencias.append("TEST: Odoo no abrió el modal por falta de pendiente "
                                    "(esperado — el Proyectado no se toca en dry run)")
                return {"status": "dry_run_ok", "ban_code": None, "estado": None,
                        "aprobada": False, "advertencias": advertencias,
                        "proyectados": proyectados, "screenshots": shots}
            raise BrowserActionError(
                "sin_pendiente — Odoo: no hay materiales con pendiente para la bandeja")

        page.wait_for_selector(".modal.show .o_field_widget[name='partner_id']",
                               state="visible", timeout=45_000)

        # ── Paso 4: ajustar la Cantidad de cada línea a la SOLICITADA ─────────
        o2m = modal.locator(".o_field_widget[name='project_tray_material_ids']")
        sin_ajustar = []
        for it in items:
            ref = str(it["ref"]).strip()
            qty = self._fmt_qty(it["cantidad"])
            idx = page.evaluate("""(ref) => {
                const cont=document.querySelector(
                    ".modal.show .o_field_widget[name='project_tray_material_ids']");
                if(!cont) return -1;
                const filas=[...cont.querySelectorAll('.o_data_row')];
                return filas.findIndex(r => ((r.textContent||'') + ' ' +
                    [...r.querySelectorAll('td')].map(td=>td.getAttribute('title')||'')
                    .join(' ')).includes('/'+ref+']'));
            }""", ref)
            if idx < 0:
                advertencias.append(f"⚠ {ref}: Odoo no lo incluyó en la bandeja")
                sin_ajustar.append(ref)
                continue
            fila = o2m.locator(".o_data_row").nth(idx)
            fila.locator("td.o_list_number").first.click()
            try:
                inp = fila.locator("input").first
                inp.wait_for(state="visible", timeout=8_000)
            except PWTimeout:
                inp = modal.locator(".o_selected_row input").first
                inp.wait_for(state="visible", timeout=8_000)
            inp.click(click_count=3)
            inp.press_sequentially(qty)
            modal.locator(".modal-title, h4").first.click()
            page.wait_for_timeout(600)
            try:
                txt = (o2m.locator(".o_data_row").nth(idx)
                       .locator("td.o_list_number").first.inner_text(timeout=3_000))
                val = self._num_celda(txt)
                if abs(val - float(it["cantidad"])) > 0.001:
                    sin_ajustar.append(ref)
                    advertencias.append(f"{ref}: la celda quedó en {txt.strip()} y no en {qty}")
                    continue
            except Exception:
                pass
            log.info(f"[ADICIONAL] {ref} → cantidad {qty}")

        if sin_ajustar and not dry_run:
            if screenshots:
                shots.append(self.screenshot("adic_abort_ajuste"))
            descartar = modal.locator("button:has-text('Descartar'), "
                                      "button:has-text('DESCARTAR')")
            if descartar.count() > 0:
                descartar.first.click()
                page.wait_for_timeout(800)
            raise BrowserActionError(
                "ajuste_cantidad — " + ", ".join(sin_ajustar) +
                " no se pudieron ajustar; bandeja DESCARTADA sin guardar. "
                "OJO: el Proyectado YA quedó subido — revisar a mano.")

        # ── Paso 5: Equipo + Fecha + Almacén ──────────────────────────────────
        p_inp = modal.locator(".o_field_widget[name='partner_id'] input").first
        p_inp.click(click_count=3)
        p_inp.fill(partner_name)
        p_inp.dispatch_event("input")
        page.wait_for_selector("ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
                               state="attached", timeout=25_000)
        self._select_autocomplete_first()

        d_inp = modal.locator(".o_field_widget[name='date'] input").first
        d_inp.click(click_count=3)
        d_inp.press_sequentially(date_str)
        d_inp.press("Enter")
        page.wait_for_timeout(500)

        l_inp = modal.locator(".o_field_widget[name='location_id'] input").first
        l_inp.click(click_count=3)
        l_inp.fill(location_search)
        l_inp.dispatch_event("input")
        page.wait_for_selector("ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
                               state="attached", timeout=25_000)
        self._select_autocomplete_first()

        if screenshots:
            shots.append(self.screenshot("adic_02_pre_guardar"))

        if dry_run:
            descartar = modal.locator("button:has-text('Descartar'), "
                                      "button:has-text('DESCARTAR')")
            if descartar.count() > 0:
                descartar.first.click()
                page.wait_for_timeout(800)
            log.info("[ADICIONAL] DRY RUN — modal descartado, nada guardado")
            return {"status": "dry_run_ok", "ban_code": None, "estado": None,
                    "aprobada": False, "advertencias": advertencias,
                    "proyectados": proyectados, "screenshots": shots}

        # ── Paso 6: GUARDAR (jamás reintentar después) ─────────────────────────
        save_btn = modal.locator("button.o_form_button_save, "
                                 "button:has-text('GUARDAR'), button:has-text('Guardar')")
        save_btn.first.click()
        try:
            page.wait_for_function("""() => {
                const modal = document.querySelector('.modal.show');
                if (!modal) return true;
                const f = modal.querySelector('.o_field_widget[name="name"]');
                if (!f) return true;
                const text = f.innerText.trim();
                return text && text !== 'New' && !text.startsWith('New');
            }""", timeout=45_000)
        except PWTimeout:
            pass
        page.wait_for_timeout(1000)
        self._dismiss_error()
        ban_code = page.evaluate("""() => {
            const modal = document.querySelector('.modal.show');
            if (!modal) return '';
            const f = modal.querySelector('.o_field_widget[name="name"]');
            return f ? f.innerText.trim() : '';
        }""")
        if not ban_code or ban_code == "New":
            ban_code = None
        estado = page.evaluate("""() => {
            const modal = document.querySelector('.modal.show');
            if (!modal) return '';
            const cur = modal.querySelector('.o_statusbar_status button.o_arrow_button_current')
                     || modal.querySelector('.o_statusbar_status .o_arrow_button.btn-primary');
            return cur ? cur.innerText.trim() : '';
        }""")

        aprobada = False
        if ban_code and aprobar_si is not None:
            try:
                autorizado = bool(aprobar_si(ban_code))
            except Exception as e:
                autorizado = False
                advertencias.append(f"Verificación pre-APROBAR falló: {e}")
            if autorizado:
                try:
                    ap = modal.locator("button[name='approve']").first
                    ap.wait_for(state="visible", timeout=10_000)
                    ap.click()
                    page.wait_for_timeout(3000)
                    self._dismiss_error()
                    if page.locator(".modal.show").count() == 0:
                        estado = "APROBADO"
                    aprobada = "APROBAD" in (estado or "").upper()
                except Exception as e:
                    advertencias.append(f"Click APROBAR falló: {e}")
            else:
                advertencias.append("Verificación pre-APROBAR NO cuadró — queda 'Para aprobar'")
        if screenshots:
            shots.append(self.screenshot("adic_03_guardada"))
        try:
            cerrar = page.locator(".modal.show button.close")
            if cerrar.count() > 0:
                cerrar.first.click()
                page.wait_for_timeout(800)
        except Exception:
            pass
        return {"status": "ok", "ban_code": ban_code, "estado": estado or "Para aprobar",
                "aprobada": aprobada, "advertencias": advertencias,
                "proyectados": proyectados, "screenshots": shots}

    def aprobar_bandeja(self, tray_id: int, ban_code: str) -> dict:
        """
        Abre el form de una bandeja existente por URL directa y pulsa APROBAR.
        Seguridad: verifica que el name del form coincida con ban_code antes
        de clicar (jamás aprobar una bandeja distinta a la esperada).
        """
        page = self._page
        self._navegar(f"/web#id={tray_id}&model=project.tray&view_type=form"
                      f"&cids=1&menu_id=352")
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_TIMEOUT)
        except PWTimeout:
            shot = self.screenshot("aprobar_nav_timeout")
            raise BrowserActionError(f"nav_timeout — form bandeja no cargó. Shot: {shot}")
        page.wait_for_timeout(800)

        name = page.evaluate("""() => {
            const f = document.querySelector('.o_form_view .o_field_widget[name="name"]');
            return f ? f.innerText.trim() : '';
        }""")
        if name != ban_code:
            raise BrowserActionError(
                f"duplicate_error — el form muestra '{name}' y se esperaba '{ban_code}': ABORTADO")

        ap = page.locator(".o_form_view button[name='approve']").first
        try:
            ap.wait_for(state="visible", timeout=10_000)
        except PWTimeout:
            estado = page.evaluate("""() => {
                const cur = document.querySelector(
                    '.o_statusbar_status button.o_arrow_button_current');
                return cur ? cur.innerText.trim() : '';
            }""")
            return {"status": "sin_boton", "estado": estado or "?",
                    "detalle": "Botón APROBAR no visible (¿ya está aprobada?)"}
        ap.click()
        page.wait_for_timeout(3000)
        self._dismiss_error()
        estado = page.evaluate("""() => {
            const cur = document.querySelector(
                '.o_statusbar_status button.o_arrow_button_current');
            return cur ? cur.innerText.trim() : '';
        }""")
        log.info(f"[APROBAR] {ban_code}: estado={estado}")
        return {"status": "ok", "estado": estado or "APROBADO"}
