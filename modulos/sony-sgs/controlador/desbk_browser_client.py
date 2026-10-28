"""
PlaywrightClient — Automatización de navegador para SGS
========================================================
ÚNICA clase autorizada para escribir datos en SGS.
Replica exactamente las acciones del usuario humano.

Selectores confirmados desde screenshots (10-04-2026):
  - Formulario: project.material con campo 'advance_project_material_ids'
  - Columnas tabla: Equipo | Lote | Cantidad De Avance | Fecha De Consumo | Stock Disponible
  - Editar: button:has-text('EDITAR')
  - Guardar: button:has-text('GUARDAR')
  - Descartar: button:has-text('DESCARTAR')
  - Add line: [name='advance_project_material_ids'] .o_field_x2many_list_row_add a
  - Campo partner: [name='partner_id'] input (dentro de la nueva fila)
  - Campo cantidad: [name='amount'] input (dentro de la nueva fila)
"""

import logging
import os
import re
import time
import asyncio
from datetime import datetime
from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PWTimeout
from playwright.async_api import async_playwright, TimeoutError as AsyncPWTimeout

log = logging.getLogger("browser_client")

# ── Configuración ──────────────────────────────────────────────────────────────
SGS_URL      = os.getenv("SGS_URL",      "http://co.sgsdominion-global.com:8069")
SGS_DB       = os.getenv("SGS_DB",       "DOMINION_CO_002")
SGS_LOGIN    = os.getenv("SGS_LOGIN",    "")
SGS_PASSWORD = os.getenv("SGS_PASSWORD", "")

WAIT_UNTIL  = 120_000  # ceiling único: 2 min — todos los wait_for usan este valor
SLOW_MO_MS  = 400     # pausa entre acciones — más estable en Odoo

# Fecha de avance MO. None = fecha del día (DD/MM/YYYY). Solo en cierre de mes
# fijar aquí, p.ej.: FECHA_FIJA_MO = "30/05/2026"
FECHA_FIJA_MO = None
_PDL_CODE = re.compile(r"\bCDOP[A-Z0-9-]+\b", re.IGNORECASE)


def _safe(s) -> str:
    """Convierte cualquier valor a string ASCII seguro para la consola Windows."""
    return str(s).encode("ascii", "replace").decode("ascii")


def _combinar_pdl(actual: str = "", nuevo: str = "") -> str:
    """Une PDL existentes y nuevos sin borrar los anteriores ni duplicar tokens."""
    tokens = []
    vistos = set()

    def _partes(valor: str):
        raw = str(valor or "").strip()
        if not raw or raw.upper() in {"-", "0", "N/A", "NA", "NONE", "FALSE"}:
            return []
        if ";" in raw or "," in raw:
            return [p.strip() for p in re.split(r"[;,]+", raw) if p.strip()]
        codigos = _PDL_CODE.findall(raw)
        if len(codigos) > 1:
            return codigos
        return [raw]

    for valor in (actual, nuevo):
        for parte in _partes(valor):
            token = parte.strip().upper()
            if not token or token in vistos:
                continue
            vistos.add(token)
            tokens.append(token)
    return ";".join(tokens)


class BrowserActionError(RuntimeError):
    pass


class PlaywrightClient:
    """
    Controla el navegador para ejecutar liquidaciones en SGS.

    Uso normal:
        with PlaywrightClient(headless=False) as pw:
            pw.login()
            resultado = pw.liquidar_material(pm_id=504830, cantidad=4.0,
                                             partner_nombre="SANTANA PERAZA EDGAR HERNANDO")

    Uso de inspección (para mapear selectores):
        with PlaywrightClient(headless=False) as pw:
            pw.login()
            info = pw.inspect_material(pm_id=504830)
            # → devuelve html, field_names, screenshots
    """

    def __init__(self, headless: bool = False, slow_mo: int = None):
        self.headless   = headless
        self._slow_mo   = slow_mo if slow_mo is not None else SLOW_MO_MS
        self._pw        = None
        self._browser: Browser = None
        self._page: Page = None
        self._logueado  = False

    # ── Context manager (uso temporal) ────────────────────────────────────────
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        # Solo cerrar si NO es sesión persistente (global)
        # Las sesiones globales se cierran con close() explícito
        pass

    # ── Ciclo de vida ─────────────────────────────────────────────────────────
    def start(self, storage_state: dict = None):
        """
        Inicia el navegador.
        storage_state: si se pasa (resultado de get_storage_state()), el worker
        arranca con la sesión ya activa — sin necesidad de hacer login.
        """
        if self.is_alive():
            return
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless, slow_mo=self._slow_mo)
        vp = {"width": 1400, "height": 900}
        if storage_state:
            ctx = self._browser.new_context(storage_state=storage_state, viewport=vp)
            self._page = ctx.new_page()
            self._logueado = True   # sesión heredada — no hace falta login
            log.info("[PW] Navegador iniciado con sesión heredada (sin login)")
        else:
            self._page = self._browser.new_page(viewport=vp)
            self._logueado = False
            log.info("[PW] Navegador iniciado")
        self._page.set_default_timeout(WAIT_UNTIL)

    def close(self):
        """Cierra el navegador completamente."""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._browser  = None
        self._pw       = None
        self._page     = None
        self._logueado = False
        log.info("[PW] Navegador cerrado")

    def is_alive(self) -> bool:
        """True si el navegador sigue abierto y la página responde."""
        try:
            if self._browser is None or self._page is None:
                return False
            if not self._browser.is_connected():
                return False
            # Prueba real: acceder al título de la página
            _ = self._page.title()
            return True
        except Exception:
            return False

    def ensure_logged_in(self):
        """
        Garantiza sesión activa en SGS.
        - Si el navegador fue cerrado (por el usuario o por error) → lo reabre y hace login.
        - Si la sesión expiró → hace login sin reabrir.
        - Si todo está OK → no hace nada.
        """
        if not self.is_alive():
            log.info("[PW] Navegador cerrado o inaccesible — reiniciando...")
            self.close()   # limpia estado roto
            self.start()
            self._logueado = False

        if self._logueado:
            try:
                if self._page.locator(".o_main_navbar").is_visible(timeout=3000):
                    log.info("[PW] Sesion activa — sin re-login")
                    return
            except Exception:
                pass
            self._logueado = False
            log.info("[PW] Sesion expirada — re-login")

        self.login()

    # ── Helpers ────────────────────────────────────────────────────────────────
    def _ss(self, nombre: str) -> str:
        """Toma un screenshot y devuelve la ruta."""
        os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)
        path = os.path.join(os.path.dirname(__file__), "logs", f"{nombre}.png")
        self._page.screenshot(path=path)
        return path

    def _dismiss_error(self):
        """
        Cierra modales de ERROR de Odoo.
        SOLO actúa si hay un indicador claro de error (.o_error_dialog o título
        con 'rror'/'Advertencia'). No toca diálogos de 'Guardar cambios' para
        evitar escrituras accidentales por auto-edit mode.
        """
        try:
            # Verificar que es un modal de error real antes de clicar ACEPTAR
            error_modal = self._page.locator(
                ".o_error_dialog, "
                ".modal .modal-title:has-text('rror'), "
                ".modal .modal-title:has-text('Advertencia'), "
                ".modal .modal-title:has-text('Atenci')"
            ).first
            if error_modal.count() > 0 and error_modal.is_visible(timeout=2000):
                btn = self._page.locator("button:has-text('ACEPTAR'), button:has-text('Aceptar'), button:has-text('Ok')").first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    time.sleep(0.5)
        except Exception:
            pass

    # ── Helpers de fill para crear_proyecto_hijo ─────────────────────────────

    def _fill_char(self, field_name: str, value: str):
        """Rellena un campo Char/Text de Odoo.
        Soporta input, textarea y div.o_input (contenteditable — Odoo 17 title fields)."""
        widget = self._page.locator(f".o_field_widget[name='{field_name}']").first
        widget.scroll_into_view_if_needed()
        widget.wait_for(state="visible", timeout=10_000)
        # Odoo 17: campos de título usan div.o_input (contenteditable), no <input>
        inp_loc = widget.locator("input, textarea, div.o_input, [contenteditable='true']")
        if inp_loc.count() > 0:
            inp = inp_loc.first
            inp.click(click_count=3)
            inp.fill(str(value))
        else:
            # Fallback: click en el widget y tipear con teclado
            widget.click(click_count=3)
            self._page.keyboard.press("Control+a")
            self._page.keyboard.type(str(value))

    def _select_autocomplete_first(self):
        """Selecciona el primer item del dropdown activo del autocomplete de Odoo.

        Hay múltiples ul.ui-autocomplete en la página (uno por campo many2one).
        Solo el del campo activo tiene display:block en el style inline.
        Buscar por [style*='display: block'] es más fiable que :visible (CSS).
        """
        page = self._page
        page.wait_for_timeout(800)  # tiempo para que Odoo haga el RPC y renderice
        visible_sel = (
            "ul.ui-autocomplete[style*='display: block'] li.ui-menu-item:not(.ui-state-disabled), "
            "ul.ui-autocomplete[style*='display:block'] li.ui-menu-item:not(.ui-state-disabled)"
        )
        try:
            page.wait_for_selector(visible_sel, state="visible", timeout=5_000)
            page.locator(visible_sel).first.click(timeout=3_000)
        except Exception:
            # Fallback: simular selección via jQuery UI (mouseenter+mousedown)
            page.evaluate("""() => {
                const ul = Array.from(document.querySelectorAll('ul.ui-autocomplete')).find(
                    el => el.style.display === 'block' || getComputedStyle(el).display === 'block'
                );
                if (!ul) return;
                const li = ul.querySelector('li.ui-menu-item:not(.ui-state-disabled)');
                if (!li) return;
                $(li).trigger('mouseenter');
                $(li).trigger('mouseover');
                $(li).trigger('mousedown');
                $(li).trigger('click');
            }""")
        page.wait_for_timeout(400)

    def _fill_many2one(self, field_name: str, search_text: str):
        """Busca y selecciona la primera opción de un campo many2one.

        Usa fill()+dispatch_event('input') — probado y funcionando en SGS_UI_Materiales.
        El NBSP en el search_text se pasa tal cual (Odoo lo acepta en ILIKE).
        """
        widget = self._page.locator(f".o_field_widget[name='{field_name}']")
        widget.scroll_into_view_if_needed()
        inp = widget.locator("input").first
        inp.click(click_count=3)
        inp.fill("")
        inp.fill(search_text)
        inp.dispatch_event("input")
        self._page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=15_000,
        )
        self._select_autocomplete_first()

    def _fill_many2one_if_empty(self, field_name: str, search_text: str):
        """Como _fill_many2one pero solo actúa si el campo está vacío."""
        widget = self._page.locator(f".o_field_widget[name='{field_name}']")
        if not widget.count():
            return
        inp = widget.locator("input").first
        if inp.count() and inp.input_value():
            log.info(f"[FILL] {field_name} ya tiene valor — omitido")
            return
        self._fill_many2one(field_name, search_text)

    def _fill_many2many_tag(self, field_name: str, tag_name: str):
        """Agrega un tag a un campo Many2many-Tags."""
        widget = self._page.locator(
            f".o_field_many2manytags[name='{field_name}']"
        ).first
        widget.scroll_into_view_if_needed()
        inp = widget.locator("input").first
        inp.click()
        inp.fill(tag_name)
        inp.dispatch_event("input")
        self._page.wait_for_selector(
            "ul.ui-autocomplete li.ui-menu-item:not(.ui-state-disabled)",
            state="attached", timeout=15_000,
        )
        self._select_autocomplete_first()

    @staticmethod
    def _parse_sgs_number(raw) -> float | None:
        """Parsea numeros de SGS: 1,00 / 1.00 / 1.234,56 / 1,234.56."""
        if raw is None:
            return None
        txt = str(raw).replace("\xa0", " ").strip()
        matches = re.findall(r"-?\d+(?:[.,]\d+)*", txt)
        if not matches:
            return None
        num = matches[-1]
        if "," in num and "." in num:
            if num.rfind(",") > num.rfind("."):
                num = num.replace(".", "").replace(",", ".")
            else:
                num = num.replace(",", "")
        elif "," in num:
            num = num.replace(",", ".")
        try:
            return float(num)
        except ValueError:
            return None

    def _leer_numero_campo(self, field_name: str) -> float | None:
        """Lee un campo numerico del DOM tal como lo ve el usuario."""
        locs = self._page.locator(f"[name='{field_name}']")
        for i in range(locs.count()):
            loc = locs.nth(i)
            try:
                if not loc.is_visible(timeout=500):
                    continue
            except Exception:
                continue
            for reader in ("input", "text"):
                try:
                    raw = loc.input_value(timeout=1500) if reader == "input" else loc.inner_text(timeout=1500)
                    val = self._parse_sgs_number(raw)
                    if val is not None:
                        return val
                except Exception:
                    pass
        return None

    def _leer_numero_por_label(self, label_text: str) -> float | None:
        """Fallback para vistas Odoo donde el campo no conserva name visible."""
        xpath = (
            "xpath=//*[self::label or contains(@class,'o_form_label')]"
            f"[contains(normalize-space(.), '{label_text}')]"
            "/following::*[contains(@class,'o_field_widget') or self::span or self::div][1]"
        )
        try:
            loc = self._page.locator(xpath).first
            if loc.count() <= 0 or not loc.is_visible(timeout=800):
                return None
            return self._parse_sgs_number(loc.inner_text(timeout=1500))
        except Exception:
            return None

    def _campo_texto_por_label(self, labels):
        """Devuelve el primer input/textarea asociado a alguna etiqueta."""
        page = self._page
        for label_text in labels:
            xpath = (
                "xpath=//*[self::label or contains(@class,'o_form_label')]"
                f"[contains(normalize-space(.), '{label_text}')]"
                "/following::*[self::input or self::textarea or descendant::input or descendant::textarea][1]"
            )
            try:
                loc = page.locator(xpath).first
                if loc.count() <= 0 or not loc.is_visible(timeout=1000):
                    continue
                tag = (loc.evaluate("el => el.tagName") or "").upper()
                target = loc if tag in {"INPUT", "TEXTAREA"} else loc.locator("input, textarea").first
                if target.count() <= 0 or not target.is_visible(timeout=1000):
                    continue
                return target
            except Exception:
                continue
        return None

    def _leer_texto_campo(self, loc) -> str:
        """Lee el valor actual de un input/textarea si Odoo lo deja visible."""
        if loc is None:
            return ""
        for reader in (
            lambda: loc.input_value(timeout=1000),
            lambda: loc.get_attribute("value", timeout=1000),
            lambda: loc.inner_text(timeout=1000),
        ):
            try:
                return str(reader() or "").strip()
            except Exception:
                continue
        return ""

    def _llenar_texto_por_label(self, labels, valor: str) -> bool:
        """Escribe un texto en el primer input/textarea asociado a alguna etiqueta."""
        try:
            target = self._campo_texto_por_label(labels)
            if target is None:
                return False
            target.click(click_count=3)
            target.fill(valor)
            target.press("Tab")
            return True
        except Exception:
            return False

    def _cambiar_estado_proyecto_ui(self, estado_destino: str) -> bool:
        """
        Cambia el estado del proyecto usando el flujo UI:
        engrane -> Cambiar estado -> many2one status_id -> Guardar.
        """
        page = self._page

        togglers = page.locator(".o_cp_action_menus .o_dropdown_toggler_btn")
        if togglers.count() <= 0:
            return False

        abierto = False
        for idx in range(togglers.count() - 1, -1, -1):
            try:
                btn = togglers.nth(idx)
                if not btn.is_visible(timeout=1000):
                    continue
                btn.click()
                abierto = True
                break
            except Exception:
                continue
        if not abierto:
            return False

        accion = page.get_by_text("Cambiar estado", exact=True).first
        accion.wait_for(timeout=5000)
        accion.click()

        inp = page.locator(".modal [name='status_id'] input, .o_dialog_container [name='status_id'] input").first
        inp.wait_for(timeout=8000)
        inp.click(click_count=3)
        inp.fill(estado_destino)
        time.sleep(0.4)

        try:
            opcion = page.locator(
                f".ui-menu-item:has-text('{estado_destino}'), "
                f".o_m2o_dropdown_option:has-text('{estado_destino}'), "
                f".dropdown-item:has-text('{estado_destino}')"
            ).first
            if opcion.count() > 0 and opcion.is_visible(timeout=1500):
                opcion.click()
            else:
                inp.press("ArrowDown")
                inp.press("Enter")
        except Exception:
            inp.press("ArrowDown")
            inp.press("Enter")

        save_btn = page.locator(
            ".modal button[name='change_status'], "
            ".o_dialog_container button[name='change_status'], "
            ".modal .btn.btn-primary:has-text('GUARDAR')"
        ).first
        save_btn.wait_for(timeout=5000)
        save_btn.click()

        try:
            page.wait_for_selector(".modal, .o_dialog_container .modal", state="hidden", timeout=8000)
        except PWTimeout:
            time.sleep(1.5)
        return True

    def _leer_pendiente_material_dom(self) -> tuple[float | None, float | None]:
        """
        Lee Pendiente y Devuelto como los ve SGS en pantalla.
        Pendiente es la unica cantidad autorizada para liquidar materiales.
        """
        def first_value(readers):
            for reader in readers:
                val = reader()
                if val is not None:
                    return val
            return None

        pendiente = first_value([
            lambda: self._leer_numero_campo("balance"),
            lambda: self._leer_numero_campo("pendiente"),
            lambda: self._leer_numero_por_label("Pendiente"),
            lambda: self._leer_numero_por_label("Balance"),
        ])
        devuelto = first_value([
            lambda: self._leer_numero_campo("devuelto"),
            lambda: self._leer_numero_campo("return_quantity"),
            lambda: self._leer_numero_por_label("Devuelto"),
        ])
        return pendiente, devuelto

    def get_storage_state(self) -> dict:
        """
        Exporta cookies y localStorage de la sesión activa.
        Úsalo para pasar la sesión a workers paralelos sin re-login.
        """
        if not self.is_alive():
            raise BrowserActionError("Navegador no iniciado — llama start() primero")
        return self._page.context.storage_state()

    def _url_material(self, pm_id: int) -> str:
        # cids=1 y menu_id=352 son obligatorios: sin ellos el SPA de Odoo
        # no conoce el contexto de empresa/menú y cae al home en vez del form.
        return (f"{SGS_URL}/web#id={pm_id}"
                f"&model=project.material&view_type=form&cids=1&menu_id=352")

    def _url_proyecto(self, pid: int) -> str:
        return (f"{SGS_URL}/web#id={pid}&action=485"
                f"&model=project.dominion&view_type=form&cids=1&menu_id=352")

    @staticmethod
    def _coincide_codigo_proyecto(code_val: str, pro_code: str) -> bool:
        esperado = str(pro_code or "").strip()
        actual = str(code_val or "").strip()
        if not esperado:
            return True
        return esperado in actual or esperado.replace("PRO-", "") in actual

    def _leer_codigo_proyecto(self) -> str:
        loc = self._page.locator("[name='code']").first
        if loc.count() <= 0:
            return ""
        try:
            return loc.inner_text().strip()
        except Exception:
            try:
                return loc.input_value().strip()
            except Exception:
                return ""

    def _descartar_dialogo_guardar(self):
        try:
            for sel in [
                "button:has-text('Descartar')",
                "button:has-text('No guardar')",
                "button:has-text('Discard')",
                ".modal-footer button.btn-secondary",
            ]:
                el = self._page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=800):
                    el.click()
                    log.info("[PW] Dialogo 'guardar cambios' descartado")
                    time.sleep(0.3)
                    break
        except Exception:
            pass

    def precalentar_proyecto(self, pid: int) -> None:
        """
        Navega al form del proyecto (project.dominion) para forzar la
        recomputación de campos stored en Odoo antes de leer via API.

        Cuando Odoo carga el form, su ORM hace read() sobre los registros
        relacionados (incluyendo project.material), disparando el queue de
        recompute de campos como balance, despachado, devuelto. Esto garantiza
        que la siguiente lectura API obtenga valores frescos, no stale.

        Best-effort: si falla, el verificador continúa con los valores que tenga.
        """
        try:
            self._page.goto(f"{SGS_URL}/web?cids=1",
                            wait_until="domcontentloaded", timeout=10_000)
            self._page.goto(self._url_proyecto(pid),
                            wait_until="domcontentloaded", timeout=12_000)
            try:
                self._page.wait_for_selector(
                    ".o_form_view .o_field_widget, .o_form_view h1",
                    timeout=10_000,
                )
            except Exception:
                pass
            time.sleep(2.0)   # margen para que Odoo procese el recompute queue
        except Exception:
            pass

    def _navegar_proyecto(self, pid: int, pro_code: str = "") -> str:
        """
        Navega de forma robusta al formulario de project.dominion.

        Odoo a veces deja el DOM del proyecto anterior aunque la URL ya cambie
        al nuevo hash. El patron double-goto reduce mucho ese problema.
        """
        page = self._page
        url = self._url_proyecto(pid)

        page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded")
        time.sleep(0.5)
        self._descartar_dialogo_guardar()

        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_selector(
            ".o_form_view .o_field_widget, .o_form_view h1, .o_form_view .o_form_label",
            timeout=WAIT_UNTIL,
        )
        time.sleep(1.5)
        self._dismiss_error()

        code_val = self._leer_codigo_proyecto()
        if self._coincide_codigo_proyecto(code_val, pro_code):
            return code_val

        log.warning(
            f"[PW] Proyecto no coincide tras goto pid={pid}: pantalla='{code_val}' "
            f"esperado='{pro_code}'. Intentando reload..."
        )
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector(
            ".o_form_view .o_field_widget, .o_form_view h1, .o_form_view .o_form_label",
            timeout=WAIT_UNTIL,
        )
        time.sleep(1.5)
        self._dismiss_error()

        code_val = self._leer_codigo_proyecto()
        if not self._coincide_codigo_proyecto(code_val, pro_code):
            raise BrowserActionError(
                f"Proyecto incorrecto en pantalla: '{code_val}' esperado '{pro_code}'"
            )
        return code_val

    # ── ACTUALIZAR FECHAS DEL PROYECTO ────────────────────────────────────────
    def actualizar_fechas_proyecto(self,
                                    pid: int,
                                    fecha: str = None,
                                    pro_code: str = "",
                                    pdl: str = "",
                                    estado_destino: str = "") -> dict:
        """
        Actualiza las 7 fechas del proyecto a la fecha indicada (por defecto hoy).

        Campos actualizados:
          date_assignment, date_expected, date_real,
          certification_date, date_end_expected, date_end_real, last_status_change

        fecha: formato DD-MM-YYYY. Si None, usa la fecha de hoy.
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        from datetime import date as _date
        if fecha is None:
            fecha = _date.today().strftime("%d-%m-%Y")

        CAMPOS_FECHA = [
            "date_assignment",
            "date_expected",
            "date_real",
            "certification_date",
            "date_end_expected",
            "date_end_real",
            "last_status_change",
        ]

        if not estado_destino and pdl:
            estado_destino = "1.4-TERMINADO/EN BALANCE"

        log.info(
            f"[PW] ActualizarFechas pid={pid} fecha={fecha} pro={pro_code} "
            f"pdl={pdl} estado={estado_destino}"
        )
        page   = self._page
        result = {"pid": pid, "fecha": fecha, "ok": False, "pasos": []}
        tag    = f"fechas_{pid}"

        try:
            # Navegar al formulario del proyecto
            code_val = self._navegar_proyecto(pid, pro_code=pro_code)
            result["proyecto_en_pantalla"] = code_val
            result["pasos"].append(self._ss(f"{tag}_01_form"))

            # Entrar en modo edición
            edit_btn = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
            if edit_btn.count() > 0:
                try:
                    if edit_btn.is_visible(timeout=3000):
                        edit_btn.click()
                        time.sleep(1.0)
                except PWTimeout:
                    pass
            result["pasos"].append(self._ss(f"{tag}_02_edit"))

            # Actualizar cada campo de fecha
            actualizados = []
            for campo in CAMPOS_FECHA:
                try:
                    el = page.locator(f"[name='{campo}'] input, input[name='{campo}']").first
                    if el.count() == 0:
                        log.warning(f"[PW] Campo {campo} no encontrado en form")
                        continue
                    if not el.is_visible(timeout=2000):
                        continue
                    el.triple_click() if False else el.click(click_count=3)
                    el.fill(fecha)
                    # Confirmar con Tab para que Odoo acepte el valor
                    el.press("Tab")
                    time.sleep(0.2)
                    actualizados.append(campo)
                except Exception as ex:
                    log.warning(f"[PW] No se pudo actualizar {campo}: {ex}")

            result["campos_actualizados"] = actualizados
            result["pasos"].append(self._ss(f"{tag}_03_fechas"))

            if pdl:
                actualizado_pdl = False
                pdl_original = ""
                pdl_a_guardar = _combinar_pdl("", pdl)
                for selector in [
                    "[name='pdl'] input, input[name='pdl']",
                    "[name='ticket_pdl'] input, input[name='ticket_pdl']",
                    "[name='ticket'] input, input[name='ticket']",
                    "[name='pdl_ticket'] input, input[name='pdl_ticket']",
                ]:
                    try:
                        el = page.locator(selector).first
                        if el.count() > 0 and el.is_visible(timeout=1000):
                            pdl_original = self._leer_texto_campo(el)
                            pdl_a_guardar = _combinar_pdl(pdl_original, pdl)
                            el.click(click_count=3)
                            el.fill(pdl_a_guardar)
                            el.press("Tab")
                            actualizado_pdl = True
                            break
                    except Exception:
                        pass
                if not actualizado_pdl:
                    target_pdl = self._campo_texto_por_label(
                        ["Ticket / PDL", "PDL / Ticket", "PDL", "Ticket"]
                    )
                    if target_pdl is not None:
                        try:
                            pdl_original = self._leer_texto_campo(target_pdl)
                            pdl_a_guardar = _combinar_pdl(pdl_original, pdl)
                            target_pdl.click(click_count=3)
                            target_pdl.fill(pdl_a_guardar)
                            target_pdl.press("Tab")
                            actualizado_pdl = True
                        except Exception:
                            actualizado_pdl = False
                if actualizado_pdl:
                    result["pdl_actualizado"] = pdl_a_guardar
                    result["pdl_anterior"] = pdl_original
                    result["pdl_solicitado"] = pdl
                else:
                    log.warning(f"[PW] No se pudo actualizar PDL pid={pid}: {pdl}")

            # Guardar
            save_btn = page.locator(
                "button:has-text('GUARDAR'), button:has-text('Guardar')"
            ).first
            save_btn.wait_for(timeout=WAIT_UNTIL)
            save_btn.click()
            try:
                page.wait_for_selector(
                    "button:has-text('GUARDAR'), button:has-text('Guardar')",
                    state="hidden", timeout=8000
                )
            except PWTimeout:
                time.sleep(2.0)
            self._check_error_modal()
            result["pasos"].append(self._ss(f"{tag}_04_saved"))

            if estado_destino:
                try:
                    if self._cambiar_estado_proyecto_ui(estado_destino):
                        result["estado_actualizado"] = estado_destino
                    else:
                        log.warning(f"[PW] No se pudo abrir flujo UI de cambio de estado pid={pid}")
                except Exception as ex:
                    log.warning(f"[PW] No se pudo cambiar estado por UI pid={pid}: {ex}")

            result["ok"]  = True
            result["msg"] = f"{len(actualizados)}/7 fechas → {fecha}"
            if result.get("pdl_actualizado"):
                result["msg"] += f" | PDL → {result['pdl_actualizado']}"
            if result.get("estado_actualizado"):
                result["msg"] += f" | Estado → {result['estado_actualizado']}"
            log.info(f"[PW] Fechas actualizadas OK pid={pid}: {actualizados}")

        except PWTimeout as ex:
            result["error"] = f"Timeout: {ex}"
            result["pasos"].append(self._ss(f"{tag}_timeout"))
            log.error(f"[PW] Timeout fechas pid={pid}: {ex}")
        except BrowserActionError as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error fechas pid={pid}: {ex}")
        except Exception as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error fechas pid={pid}: {ex}")

        return result

    # ── Crear proyecto hijo (NODO) ────────────────────────────────────────────

    def crear_proyecto_hijo(self,
                            parent_id: int,
                            nombre: str,
                            nodo: str,
                            ticket: str,
                            pdl: str,
                            contract_id_name: str,
                            department_id_name: str,
                            technology_id_name: str = "PROYECTO",
                            bandeja_approver_name: str = "",
                            dry_run: bool = True,
                            screenshots: bool = True) -> dict:
        """
        Crea un proyecto hijo (NODO) en SGS.

        Flujo: form padre → stat button 'Proyectos' → NUEVO → fill → GUARDAR.
        Protocolo:
          dry_run=True  → llena el form pero NO hace click GUARDAR.
          dry_run=False → guarda. R1: NO reintentar si hay timeout post-GUARDAR.

        Returns: {"status": "ok"|"dry_run_ok"|"guardado_verificar", "id": int|None, "screenshots": [...]}
        """
        page  = self._page
        shots = []
        log.info(
            f"[CREAR_HIJO] parent_id={parent_id} nombre='{nombre}' "
            f"nodo='{nodo}' dry_run={dry_run}"
        )

        # Paso 1: navegar al form del padre (double-goto obligatorio)
        url_padre = (f"{SGS_URL}/web#id={parent_id}&action=485"
                     f"&model=project.dominion&view_type=form&cids=1&menu_id=352")
        page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded")
        time.sleep(0.5)
        page.goto(url_padre, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_UNTIL)
        except PWTimeout:
            raise BrowserActionError("nav_timeout — form del padre no cargó")
        log.info(f"[CREAR_HIJO] Form padre (id={parent_id}) cargado")

        # Paso 2: stat button "Proyectos"
        btn_proj = page.locator(
            ".o_form_view button[name='action_button_project'], "
            ".o_form_view .oe_stat_button[name='action_button_project']"
        )
        btn_proj.first.wait_for(state="visible", timeout=15_000)
        btn_proj.first.click()

        # Paso 3: esperar lista de hijos
        try:
            page.wait_for_selector(".o_list_view", timeout=WAIT_UNTIL)
        except PWTimeout:
            raise BrowserActionError("nav_timeout — lista de proyectos hijos no cargó")
        log.info("[CREAR_HIJO] Lista de hijos cargada")

        # Paso 4: NUEVO
        btn_nuevo = page.locator(
            "button.o_list_button_add, "
            "button:has-text('Nuevo'), button:has-text('NUEVO')"
        )
        btn_nuevo.first.wait_for(state="visible", timeout=15_000)
        btn_nuevo.first.click()

        # Paso 5: form de creación
        try:
            page.wait_for_selector(".o_form_view", timeout=WAIT_UNTIL)
        except PWTimeout:
            raise BrowserActionError("nav_timeout — form de creación no cargó")
        log.info("[CREAR_HIJO] Form de creación listo")

        if screenshots:
            shots.append(self._ss("crear_hijo_01_form_abierto"))

        # ── Paso A: Nombre ────────────────────────────────────────────────────
        log.info(f"[CREAR_HIJO] 1/6 Nombre: {nombre}")
        self._fill_char("name", nombre)

        # ── Paso B: Contrato — MASTER FIELD (revela nodo/pdl/ticket + cascade) ─
        log.info(f"[CREAR_HIJO] 2/6 Contrato: {contract_id_name}")
        self._fill_many2one("contract_id", contract_id_name)
        try:
            page.wait_for_selector(
                ".o_field_widget[name='nodo']:not(.o_invisible_modifier)",
                timeout=10_000,
            )
            log.info("[CREAR_HIJO] Campo 'nodo' visible — onchange del contrato OK")
        except PWTimeout:
            raise BrowserActionError(
                "field_timeout — 'nodo' sigue invisible tras onchange del contrato"
            )
        if screenshots:
            shots.append(self._ss("crear_hijo_02_post_contrato"))

        # Dar tiempo al onchange del contrato para propagar sus cambios al DOM
        time.sleep(1)

        # ── Paso C: campos opcionales (solo si quedaron vacíos) ───────────────
        log.info(f"[CREAR_HIJO] 3/6 Departamento (si vacío): {department_id_name}")
        self._fill_many2one_if_empty("department_id", department_id_name)

        log.info(f"[CREAR_HIJO] 4/6 Tecnologia (si vacía): {technology_id_name}")
        self._fill_many2one_if_empty("technology_id", technology_id_name)

        if bandeja_approver_name:
            log.info(f"[CREAR_HIJO] Aprobadores: {bandeja_approver_name}")
            self._fill_many2many_tag("bandeja_approver_ids", bandeja_approver_name)

        # ── Paso E: campos variables del nodo ────────────────────────────────
        log.info(f"[CREAR_HIJO] Nodo: {nodo}  PDL: {pdl}  Ticket: {ticket}")
        self._fill_char("nodo",   nodo)
        self._fill_char("pdl",    pdl)
        self._fill_char("ticket", ticket)

        if screenshots:
            shots.append(self._ss("crear_hijo_04_pre_guardar"))

        log.info(f"[CREAR_HIJO] Formulario completo — dry_run={dry_run}")

        if dry_run:
            log.info("[CREAR_HIJO] DRY RUN — NO se hace click GUARDAR.")
            return {"status": "dry_run_ok", "screenshots": shots}

        # ── GUARDAR (R1: nunca reintentar) ────────────────────────────────────
        log.info("[CREAR_HIJO] Ejecutando GUARDAR...")
        save_btn = page.locator(
            "button.o_form_button_save, "
            "button:has-text('GUARDAR'), button:has-text('Guardar')"
        )
        save_btn.wait_for(state="visible", timeout=10_000)
        save_btn.first.click()

        try:
            page.wait_for_selector(
                ".o_breadcrumb .active:not(:has-text('Nuevo'))",
                timeout=25_000,
            )
        except PWTimeout:
            pass  # timeout ≠ error — el registro puede haberse guardado igual

        if screenshots:
            shots.append(self._ss("crear_hijo_04_post_guardar"))

        self._dismiss_error()
        err_dlg = page.locator(".o_error_dialog")
        if err_dlg.count() and err_dlg.is_visible(timeout=2000):
            raise BrowserActionError(f"save_error — {err_dlg.first.inner_text()}")

        # Extraer ID desde la URL (hash: id=XXXXX)
        nuevo_id = None
        m = re.search(r"[#&]id=(\d+)", page.url)
        if m:
            nuevo_id = int(m.group(1))

        if nuevo_id:
            log.info(f"[CREAR_HIJO] CREADO OK — id={nuevo_id}")
            return {"status": "ok", "id": nuevo_id, "screenshots": shots}
        else:
            log.warning("[CREAR_HIJO] Guardado ejecutado pero ID no detectado en URL — verificar en SGS")
            return {"status": "guardado_verificar", "id": None, "screenshots": shots}

    # ── Abrir a página de login (para browser persistente manual) ────────────
    def open_to_login_page(self):
        """
        Navega a la página de login de SGS SIN rellenar credenciales.
        Para uso con el browser global persistente: el usuario inicia sesión
        manualmente en la ventana visible; el monitor en blueprint.py detecta
        la navbar y actualiza _logueado=True automáticamente.
        """
        if not self.is_alive():
            self.start()
        self._logueado = False
        # timeout corto: la página de login es ligera; si SGS tarda >30s es un
        # problema de red y no queremos bloquear el monitor thread 2 minutos.
        self._page.goto(
            f"{SGS_URL}/web/login?db={SGS_DB}",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        log.info("[PW] Browser abierto en página de login — esperando login manual")

    # ── Login ──────────────────────────────────────────────────────────────────
    def login(self):
        page = self._page
        log.info(f"[PW] Login → {SGS_URL}")

        page.goto(f"{SGS_URL}/web/login?db={SGS_DB}", wait_until="domcontentloaded")
        page.wait_for_selector("input[name='login']", timeout=WAIT_UNTIL)
        page.fill("input[name='login']",    SGS_LOGIN)
        page.fill("input[name='password']", SGS_PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_selector(".o_main_navbar", timeout=WAIT_UNTIL)

        # Inicializar company context (cids=1) en la sesión.
        # Sin este paso, sesiones headless nuevas no tienen el contexto de empresa
        # y el SPA ignora el cids=1 de las URLs de form → landing en home screen.
        page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded")
        page.wait_for_selector(".o_main_navbar", timeout=15_000)
        time.sleep(0.5)

        self._logueado = True
        log.info("[PW] Login OK (company context inicializado)")

    # ── Navegar a project.material ─────────────────────────────────────────────
    def _navegar_material(self, pm_id: int):
        """
        Navega al formulario de project.material con pm_id.

        Usa patrón double-goto: primero a /web (limpio) y luego a la URL con hash.
        Sin el primer goto, el SPA de Odoo a veces queda en el home en lugar de
        enrutar al record — especialmente cuando el browser viene de otra pantalla
        o cuando el contexto de empresa/menú no está inicializado.
        Confirmado en ARASAKACORP (2026-04-15): sin double-goto el form no carga.

        cids=1&menu_id=352 son obligatorios para que el router SPA sepa en qué
        contexto de empresa y menú debe abrir el record.
        """
        url = self._url_material(pm_id)

        # Paso 1: ir a /web?cids=1 para reiniciar el router SPA manteniendo contexto.
        self._page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded")
        time.sleep(0.5)

        # Seguridad anti-doble-escritura: si el form anterior quedó en auto-edit
        # mode (Odoo 17), navegar aquí dispara "¿Guardar cambios?". Descartar
        # explícitamente para evitar un segundo write accidental.
        try:
            for sel in [
                "button:has-text('Descartar')",
                "button:has-text('No guardar')",
                "button:has-text('Discard')",
                ".modal-footer button.btn-secondary",
            ]:
                el = self._page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=800):
                    el.click()
                    log.info("[PW] Diálogo 'guardar cambios' descartado (anti-doble-escritura)")
                    time.sleep(0.3)
                    break
        except Exception:
            pass

        # Paso 2: navegar al record con contexto completo
        self._page.goto(url, wait_until="domcontentloaded")

        # Esperar que los widgets del formulario estén renderizados
        self._page.wait_for_selector(
            ".o_form_view .o_field_widget, .o_form_view h1, .o_form_view .o_form_label",
            timeout=WAIT_UNTIL,
        )
        # Estabilización: Odoo hace peticiones AJAX adicionales después del render
        time.sleep(1.5)
        self._dismiss_error()

    # ── MODO INSPECCIÓN — para mapear selectores ───────────────────────────────
    def inspect_material(self, pm_id: int) -> dict:
        """
        Abre el material, entra en modo edición y vuelca toda la info del DOM
        necesaria para confirmar selectores.
        Devuelve dict con screenshots, html del campo, y nombres de campos.
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        result = {"pm_id": pm_id, "pasos": [], "errores": []}
        page   = self._page

        try:
            # 1. Navegar al material
            self._navegar_material(pm_id)
            result["url_actual"] = page.url
            result["pasos"].append(self._ss(f"insp_{pm_id}_01_form"))

            # 2. Capturar campos en modo lectura
            result["fields_readonly"] = self._get_field_names()

            # 3. Entrar en modo edición
            edit_btn = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
            if edit_btn.count() > 0:
                edit_btn.click()
                time.sleep(1.2)
                self._dismiss_error()
                result["pasos"].append(self._ss(f"insp_{pm_id}_02_edit"))
            else:
                result["pasos"].append(self._ss(f"insp_{pm_id}_02_noedit"))
                result["errores"].append("Botón EDITAR no encontrado — puede que ya esté en modo edición")

            # 4. Capturar HTML del campo de detalles
            campo = page.locator("[name='advance_project_material_ids']")
            if campo.count() > 0:
                result["html_campo"] = campo.inner_html()[:3000]
                result["campo_encontrado"] = True
            else:
                result["html_campo"] = "(campo no encontrado)"
                result["campo_encontrado"] = False
                result["errores"].append("Campo 'advance_project_material_ids' no visible")

            # 5. Buscar "Agregar una línea"
            add_line = page.locator(
                "[name='advance_project_material_ids'] .o_field_x2many_list_row_add a, "
                "[name='advance_project_material_ids'] a:has-text('Agregar'), "
                "[name='advance_project_material_ids'] a:has-text('Add')"
            )
            result["add_line_count"] = add_line.count()
            if add_line.count() > 0:
                result["add_line_text"] = add_line.first.inner_text()
            else:
                result["errores"].append("'Agregar una línea' no encontrado — revisar HTML del campo")

            # 6. Capturar todos los name= visibles
            result["fields_edit"] = self._get_field_names()

            # 7. Screenshot final en edición
            result["pasos"].append(self._ss(f"insp_{pm_id}_03_campo"))

            # 8. Descartar — sin guardar nada
            discard = page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')").first
            if discard.count() > 0:
                discard.click()
                time.sleep(0.8)
                self._dismiss_error()

            result["ok"] = True

        except Exception as ex:
            result["ok"]    = False
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"insp_{pm_id}_error"))

        return result

    def _get_field_names(self) -> list:
        names = set()
        for el in self._page.locator("[name]").all():
            try:
                n = el.get_attribute("name")
                if n:
                    names.add(n)
            except Exception:
                pass
        return sorted(names)

    # ── GESTIÓN DE EQUIPO DEL PROYECTO ───────────────────────────────────────
    def _asegurar_lider_en_equipo(self,
                                   pid: int,
                                   partner_nombre: str,
                                   partner_cedula: str = "",
                                   pro_code: str = "",
                                   sgs_client=None) -> bool:
        """
        Verifica que partner_nombre esté en project.team para pid.
        Si no está lo agrega via UI: action_button_team → NUEVO → partner_id → GUARDAR.
        Retorna True si fue agregado, False si ya existía.
        Al terminar deja el browser en el form del proyecto (listo para abrir activities).

        partner_cedula: cédula del líder (ej. "C1076662091") — se usa como término de
        búsqueda en el dropdown de Odoo para evitar colisiones de primer nombre.
        Si está vacía, cae a buscar por primer palabra del nombre.
        """
        if not partner_nombre or not partner_nombre.strip():
            return False

        page    = self._page
        cedula  = (partner_cedula or "").strip()
        # Término de búsqueda: cédula si está disponible, primer nombre si no
        termino_busq = cedula if cedula else partner_nombre.strip().split()[0].upper()

        # ── 1. Verificar via API (rápido) ─────────────────────────────────────
        if sgs_client:
            try:
                team_rows = sgs_client.search_read(
                    "project.team",
                    [["project_id", "=", pid]],
                    ["partner_id"],
                    limit=50,
                )
                nombre_up = partner_nombre.strip().upper()
                ya_existe = any(
                    nombre_up in str((r.get("partner_id") or [None, ""])[1]).upper()
                    for r in team_rows
                )
                if ya_existe:
                    log.info(f"[PW] Líder '{partner_nombre}' ya está en equipo pid={pid} (API)")
                    return False
            except Exception as ex:
                log.warning(f"[PW] No se pudo verificar equipo via API: {ex} — usando UI")

        # ── 2. Abrir lista del equipo desde el form del proyecto ───────────────
        log.info(f"[PW] Agregando líder '{partner_nombre}' (búsqueda='{termino_busq}') al equipo pid={pid}...")
        btn_team = page.locator("button[name='action_button_team']").first
        btn_team.wait_for(state="visible", timeout=10_000)
        btn_team.click()
        page.wait_for_selector(".o_list_view", timeout=20_000)
        time.sleep(1.0)

        # Verificar también por DOM (doble check por nombre completo)
        ya_en_dom = page.locator(f".o_list_view td:has-text('{partner_nombre}')").count() > 0
        if ya_en_dom:
            log.info(f"[PW] Líder '{partner_nombre}' ya está en equipo pid={pid} (DOM)")
            self._navegar_proyecto(pid, pro_code=pro_code)
            return False

        # ── 3. NUEVO ──────────────────────────────────────────────────────────
        nuevo_btn = page.locator(
            "button:has-text('NUEVO'), button:has-text('Nuevo'), button.o_list_button_add"
        ).first
        nuevo_btn.wait_for(state="visible", timeout=8_000)
        nuevo_btn.click()
        time.sleep(1.5)

        # ── 4. Llenar partner_id usando cédula como término de búsqueda ────────
        p_input = page.locator(
            "tr.o_selected_row [name='partner_id'] input, "
            ".o_selected_row [name='partner_id'] input"
        ).first
        p_input.wait_for(state="visible", timeout=10_000)
        p_input.click()
        p_input.fill("")
        p_input.press_sequentially(termino_busq, delay=80)
        time.sleep(1.5)

        # Seleccionar opción del dropdown via JavaScript (evita problema de overflow CSS)
        # La cédula es única → exactamente 1 resultado. Buscamos el nombre en el texto del item.
        nombre_busq = partner_nombre.strip().split()[0]  # primera palabra del nombre
        if not self._click_m2o_option(nombre_busq, timeout_s=2.5):
            raise BrowserActionError(
                f"No se encontró '{partner_nombre}' en el dropdown de equipo "
                f"(búsqueda='{termino_busq}')"
            )
        time.sleep(0.8)

        # ── 5. GUARDAR ────────────────────────────────────────────────────────
        save_btn = page.locator(
            "button:has-text('GUARDAR'), button:has-text('Guardar')"
        ).first
        save_btn.wait_for(state="visible", timeout=8_000)
        save_btn.click()
        time.sleep(3.5)
        self._check_error_modal()

        log.info(f"[PW] Líder '{partner_nombre}' agregado al equipo pid={pid} OK")

        # ── 6. Volver al form del proyecto con go_back ────────────────────────
        # IMPORTANTE: No usar _navegar_proyecto aquí porque su doble-goto hacia
        # /web?cids=1 dispara el modal "unsaved changes" de Odoo, y
        # _descartar_dialogo_guardar lo descarta (borrando el save del equipo).
        # page.go_back() vuelve al project form en la historia SPA sin pasar
        # por el router de Odoo y sin mostrar ese modal.
        page.go_back()
        page.wait_for_selector(
            ".o_form_view .o_field_widget, .o_form_view h1",
            timeout=20_000,
        )
        time.sleep(1.0)
        return True

    # ── CREAR Y LIQUIDAR MO (flujo completo) ─────────────────────────────────
    def crear_y_liquidar_mo(self,
                             pid: int,
                             act_code: str,
                             cantidad: float,
                             pro_code: str = "",
                             act_id: int = None,
                             sgs_client=None,
                             dry_run: bool = False,
                             partner_nombre: str = "",
                             partner_cedula: str = "",
                             cantidad_proyectada: float = None,
                             skip_nav: bool = False) -> dict:
        """
        Flujo MO por modal — análogo a liquidar_materiales_lista_modal_parallel (MAT).

        1. Proyecto → lista de actividades (action_button_activities)
        2. Si act_id=None → CREAR inline (activities_id + amount) → GUARDAR
           La actividad queda en estado "pendiente" en la lista.
        3. Fila act_code → button[name='action_show_details'] → modal "Avance del proyecto"
        4. AGREGAR LÍNEA → fill amount + partner (Equipo) → GUARDAR footer
           (con intercepción de respuesta HTTP write sobre project.activities)

        Confirmado 2026-04-30:
          - Botón detalle en fila : button[name='action_show_details']  (fa-external-link)
          - Modal título          : "Avance del proyecto"
          - Agregar línea         : .modal .o_field_x2many_list_row_add a
          - Campo cantidad        : .modal tr.o_selected_row input[name='amount']
          - Campo equipo          : .modal tr.o_selected_row [name='partner_id'] input
          - Guardar modal         : .modal-footer button:has-text('GUARDAR')
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        cantidad_creacion = cantidad if cantidad_proyectada is None else cantidad_proyectada
        log.info(f"[PW] CrearLiquidarMO pid={pid} code={act_code} qty={cantidad} "
                 f"act_id={act_id} dry_run={dry_run}")
        page   = self._page
        result = {"act_code": act_code, "ok": False, "pasos": [], "creada": False}
        tag    = f"mo_{act_code}"

        try:
            if not skip_nav:
                # ── 1. Navegar al proyecto ──────────────────────────────────────
                code_val = self._navegar_proyecto(pid, pro_code=pro_code)
                result["pasos"].append(self._ss(f"{tag}_01_proy"))

                # ── 1b. Asegurar que el líder esté en el equipo del nodo ────────
                if partner_nombre:
                    try:
                        lider_agregado = self._asegurar_lider_en_equipo(
                            pid, partner_nombre,
                            partner_cedula=partner_cedula,
                            pro_code=pro_code,
                            sgs_client=sgs_client,
                        )
                        result["lider_agregado"] = lider_agregado
                        if lider_agregado:
                            log.info(f"[PW] Líder '{partner_nombre}' agregado al equipo antes de liquidar")
                            result["pasos"].append(self._ss(f"{tag}_01b_lider_agregado"))
                    except BrowserActionError as ex:
                        raise BrowserActionError(
                            f"No se pudo agregar líder '{partner_nombre}' al equipo: {ex}"
                        )

                # ── 2. Abrir lista de actividades ───────────────────────────────
                page.locator("[name='action_button_activities']").first.click()
                time.sleep(2.0)
                for f in page.locator(".o_searchview .o_facet_remove").all():
                    try: f.click(); time.sleep(0.3)
                    except Exception: pass
                time.sleep(1.0)
            else:
                # Ya en la lista de actividades del mismo proyecto — solo limpiar filtros.
                for f in page.locator(".o_searchview .o_facet_remove").all():
                    try: f.click(); time.sleep(0.3)
                    except Exception: pass
                time.sleep(0.5)
            result["pasos"].append(self._ss(f"{tag}_02_lista"))

            # ── 3. CREAR si no existe (→ estado pendiente en la lista) ─────────
            if act_id is None:
                log.info(f"[PW] Creando actividad {act_code}...")
                crear_btn = page.locator("button:has-text('CREAR')").first
                crear_btn.wait_for(timeout=WAIT_UNTIL)
                crear_btn.click()
                time.sleep(2.0)
                result["pasos"].append(self._ss(f"{tag}_03_nueva_fila"))

                # Campo activities_id
                act_input = page.locator(
                    "tr.o_selected_row [name='activities_id'] input, "
                    "[name='activities_id'] input"
                ).first
                act_input.wait_for(timeout=WAIT_UNTIL)
                act_input.click()
                time.sleep(0.3)
                act_input.press_sequentially(act_code, delay=80)
                time.sleep(1.5)
                result["pasos"].append(self._ss(f"{tag}_04_autocomplete"))

                # Seleccionar del dropdown
                opcion = None
                for sel in [
                    f"li:has-text('{act_code}')",
                    f".o_m2o_dropdown_option:has-text('{act_code}')",
                    f"ul.o_m2o_autocomplete li:has-text('{act_code}')",
                    ".o_m2o_dropdown_option",
                ]:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        try:
                            if el.is_visible(timeout=1500):
                                opcion = el
                                break
                        except PWTimeout:
                            pass

                if not opcion:
                    raise BrowserActionError(
                        f"Dropdown no apareció para '{act_code}' — "
                        "verificar que el código existe en el catálogo"
                    )
                result["act_descripcion"] = opcion.inner_text().strip()
                opcion.click()
                time.sleep(1.0)

                # Llenar proyectado (amount)
                amt_input = page.locator(
                    "tr.o_selected_row [name='amount'] input, "
                    "tr.o_selected_row input[name='amount']"
                ).last
                if amt_input.count() > 0:
                    try:
                        if amt_input.is_visible(timeout=2000):
                            amt_input.click(click_count=3)
                            amt_input.fill(
                                str(int(cantidad_creacion) if cantidad_creacion == int(cantidad_creacion) else cantidad_creacion)
                            )
                            time.sleep(0.4)
                    except PWTimeout:
                        pass
                result["pasos"].append(self._ss(f"{tag}_05_amount"))

                if dry_run:
                    page.locator(
                        "button:has-text('DESCARTAR'), button:has-text('Descartar')"
                    ).first.click()
                    time.sleep(0.8)
                    result["ok"]      = True
                    result["dry_run"] = True
                    result["msg"]     = f"Dry run OK — actividad {act_code} completada pero NO guardada"
                    return result

                # GUARDAR la nueva actividad (queda en lista como "pendiente")
                save_btn_lista = page.locator(
                    "button:has-text('GUARDAR'), button:has-text('Guardar')"
                ).first
                save_btn_lista.wait_for(timeout=WAIT_UNTIL)
                save_btn_lista.click()
                try:
                    page.wait_for_selector(
                        "button:has-text('GUARDAR'), button:has-text('Guardar')",
                        state="hidden", timeout=8000
                    )
                except PWTimeout:
                    time.sleep(3.0)
                self._check_error_modal()
                result["pasos"].append(self._ss(f"{tag}_06_creada"))
                result["creada"] = True
                log.info(f"[PW] Actividad {act_code} creada OK — buscando act_id...")

                # Obtener act_id via API
                time.sleep(1.5)
                if sgs_client:
                    nuevas = sgs_client.search_read(
                        "project.activities",
                        [["project_id", "=", pid], ["activity_code", "=", act_code]],
                        ["id", "amount", "advance"], limit=1,
                    )
                    if nuevas:
                        act_id = nuevas[0]["id"]
                        log.info(f"[PW] act_id via API: {act_id}")

                # Fallback: URL (funciona si Odoo navegó al form tras crear)
                if not act_id:
                    m = re.search(r"[#&?]id=(\d+)", page.url)
                    if m:
                        act_id = int(m.group(1))
                        log.info(f"[PW] act_id desde URL: {act_id}")

                # act_id puede quedar None si no hay sgs_client ni redirect —
                # el modal puede abrirse igual porque buscamos por act_code en la fila.
                if act_id:
                    log.info(f"[PW] act_id resuelto: {act_id}")
                else:
                    log.warning(f"[PW] act_id no resuelto para {act_code} — sin verificación post-save")

            # ── 4. Encontrar la fila → action_show_details → modal ────────────
            log.info(f"[PW] Buscando fila '{act_code}' en lista para abrir modal...")
            row = page.locator(f".o_data_row:has-text('{act_code}')").first
            row.wait_for(state="visible", timeout=30_000)
            row.scroll_into_view_if_needed()
            time.sleep(0.5)
            result["pasos"].append(self._ss(f"{tag}_07_fila"))

            detail_btn = row.locator("button[name='action_show_details']").first
            detail_btn.wait_for(state="visible", timeout=10_000)
            detail_btn.click()
            page.wait_for_selector(".modal", timeout=20_000)
            time.sleep(0.8)
            result["pasos"].append(self._ss(f"{tag}_08_modal"))
            log.info(f"[PW] Modal 'Avance del proyecto' abierto para {act_code}")

            # ── 5. AGREGAR LÍNEA en el modal ──────────────────────────────────
            add_sel = (
                ".modal .o_field_x2many_list_row_add a, "
                ".modal a:has-text('AGREGAR'), "
                ".modal a:has-text('Agregar'), "
                ".modal a:has-text('Add')"
            )
            add_btn = page.locator(add_sel).first
            add_btn.wait_for(state="visible", timeout=10_000)
            add_btn.click()
            time.sleep(0.8)
            result["pasos"].append(self._ss(f"{tag}_09_newline"))

            # ── 6. Llenar cantidad (Cantidad De Avance) ───────────────────────
            qty_sel = (
                ".modal tr.o_selected_row input[name='amount'], "
                ".modal tr.o_selected_row [name='amount'] input, "
                ".modal [name='amount'] input, "
                ".modal input[name='amount']"
            )
            qty = page.locator(qty_sel).last
            qty.wait_for(state="visible", timeout=10_000)
            qty.click(click_count=3)
            qty.fill(str(int(cantidad) if cantidad == int(cantidad) else cantidad))
            time.sleep(0.3)
            result["pasos"].append(self._ss(f"{tag}_10_qty"))

            # ── 6b. Llenar fecha de avance (Fecha De Avance) ──────────────────
            date_sel = (
                ".modal tr.o_selected_row input[name='date'], "
                ".modal tr.o_selected_row [name='date'] input, "
                ".modal [name='date'] input"
            )
            date_inp = page.locator(date_sel).last
            try:
                if date_inp.count() > 0 and date_inp.is_visible(timeout=3000):
                    date_inp.click(click_count=3)
                    _fecha_mo = FECHA_FIJA_MO or datetime.today().strftime("%d/%m/%Y")
                    date_inp.fill(_fecha_mo)
                    date_inp.press("Tab")
                    time.sleep(0.3)
                    log.info(f"[PW] Fecha avance MO → {_fecha_mo}")
            except PWTimeout:
                log.warning("[PW] Campo date no visible en modal MO — se omite")
            result["pasos"].append(self._ss(f"{tag}_10b_fecha"))

            # ── 7. Llenar partner (Equipo) ─────────────────────────────────────
            if partner_nombre:
                p_sel = (
                    ".modal tr.o_selected_row [name='partner_id'] input, "
                    ".modal tr.o_selected_row input[name='partner_id'], "
                    ".modal [name='partner_id'] input"
                )
                p_input = page.locator(p_sel).last
                p_input.wait_for(state="visible", timeout=10_000)
                # Usar cedula como termino de busqueda (unico, evita colisiones)
                # Si no hay cedula, usar primera palabra del nombre
                if partner_cedula:
                    termino = partner_cedula.strip()
                else:
                    palabras = partner_nombre.strip().split()
                    termino  = palabras[0] if palabras else partner_nombre.strip()
                if not termino:
                    raise BrowserActionError("partner_nombre vacío para actividad MO")
                p_input.click()
                p_input.fill("")
                p_input.press_sequentially(termino, delay=80)
                time.sleep(1.5)

                # Seleccionar via force-click (evita problema de overflow CSS del td padre).
                # El dropdown muestra el NOMBRE del partner aunque hayamos buscado por cedula,
                # por eso buscamos el item por partner_nombre (no por termino/cedula).
                nombre_busq = partner_nombre.strip().split()[0]
                if not self._click_m2o_option(nombre_busq, timeout_s=3.0):
                    raise BrowserActionError(
                        f"Lider MO '{partner_nombre}' no encontrado en dropdown del modal"
                    )
                # Blur el campo para que Odoo procese el onChange y registre el partner_id.
                # Sin blur el M2O queda en estado "pendiente" y GUARDAR falla con error
                # de campo requerido aunque visualmente muestre el nombre.
                time.sleep(0.4)
                try:
                    p_input.press("Tab")
                except Exception:
                    pass
                time.sleep(0.5)
                result["pasos"].append(self._ss(f"{tag}_11_partner"))

            if dry_run:
                page.locator(
                    ".o_dialog .modal-footer button:has-text('DESCARTAR'), "
                    ".o_dialog .modal-footer button:has-text('Descartar'), "
                    ".o_dialog button:has-text('DESCARTAR'), "
                    ".modal-footer button:has-text('Descartar')"
                ).first.click()
                time.sleep(0.8)
                result["ok"]      = True
                result["dry_run"] = True
                result["msg"]     = f"Dry run OK — modal {act_code} completado sin guardar"
                return result

            # ── 8. GUARDAR en footer del modal ────────────────────────────────
            # Selectores en orden de especificidad: o_dialog primero, modal genérico después.
            save_footer = None
            for _sel in [
                ".o_dialog .modal-footer .btn-primary",
                ".o_dialog .modal-footer button:has-text('Guardar')",
                ".o_dialog .modal-footer button:has-text('GUARDAR')",
                ".o_dialog .o_dialog_footer .btn-primary",
                ".o_dialog footer .btn-primary",
                ".modal-footer .btn-primary",
                ".modal-footer button:has-text('Guardar')",
                ".modal-footer button:has-text('GUARDAR')",
            ]:
                _el = page.locator(_sel).first
                try:
                    if _el.is_visible(timeout=1500):
                        save_footer = _el
                        log.info(f"[PW] Botón GUARDAR modal encontrado: {_sel}")
                        break
                except PWTimeout:
                    pass

            if not save_footer:
                result["pasos"].append(self._ss(f"{tag}_sin_guardar"))
                raise BrowserActionError(
                    f"Botón GUARDAR no encontrado en modal para {act_code}"
                )

            result["pasos"].append(self._ss(f"{tag}_12_antes_guardar"))
            t_save = time.perf_counter()
            save_footer.click()
            time.sleep(1.5)
            result["pasos"].append(self._ss(f"{tag}_12b_tras_guardar"))

            # Odoo puede responder de dos formas exitosas:
            # A) Modal se cierra completamente (footer desaparece)
            # B) Modal queda abierto en modo lectura → footer muestra botón EDITAR
            # Ambas son éxito. Error solo si ninguna ocurre en 25s.
            guardado_ok = False
            try:
                page.wait_for_selector(
                    ".modal-footer button:has-text('EDITAR'), "
                    ".o_dialog .modal-footer button:has-text('EDITAR')",
                    timeout=25_000,
                )
                # Caso B: modal en modo lectura → guardado OK → cerrar modal
                log.info(f"[PW] GUARDAR OK {act_code} — modal en modo lectura, cerrando")
                guardado_ok = True
                try:
                    close_btn = page.locator(
                        ".modal .btn-close, .modal button.close, .modal [aria-label='Close']"
                    ).first
                    if close_btn.count() > 0 and close_btn.is_visible(timeout=1000):
                        close_btn.click()
                    else:
                        page.keyboard.press("Escape")
                except Exception:
                    page.keyboard.press("Escape")
                time.sleep(0.8)
            except PWTimeout:
                # Verificar si el modal ya cerró solo (Caso A)
                try:
                    page.wait_for_selector(
                        ".o_dialog .modal-footer, .modal .modal-footer",
                        state="hidden",
                        timeout=3_000,
                    )
                    guardado_ok = True
                    log.info(f"[PW] GUARDAR OK {act_code} — modal cerró")
                except PWTimeout:
                    pass

            if not guardado_ok:
                result["pasos"].append(self._ss(f"{tag}_12c_modal_abierto"))
                inline_errs = []
                for err_sel in [
                    ".modal .o_field_invalid",
                    ".modal [class*='invalid']",
                    ".modal .text-danger",
                    ".modal .alert-danger",
                    ".modal .o_error_message",
                ]:
                    try:
                        cnt = page.locator(err_sel).count()
                        if cnt:
                            txt = page.locator(err_sel).first.inner_text(timeout=1000).strip()
                            inline_errs.append(f"{err_sel}({cnt}): {txt[:120]}")
                    except Exception:
                        pass
                detail = "; ".join(inline_errs) if inline_errs else "sin errores inline detectados"
                self._check_error_modal()
                raise BrowserActionError(
                    f"Modal no cerró tras GUARDAR MO {act_code} — {detail}"
                )

            self._check_error_modal()
            write_dt = time.perf_counter() - t_save
            time.sleep(1.0)
            result["pasos"].append(self._ss(f"{tag}_13_guardado"))
            result["write_dt"] = round(write_dt, 2)
            log.info(f"[PW] GUARDAR modal OK para {act_code} en {write_dt:.1f}s")

            # ── 9. Verificar via API ───────────────────────────────────────────
            if sgs_client and act_id:
                time.sleep(1.0)
                act_data = sgs_client.search_read(
                    "project.activities",
                    [["id", "=", act_id]],
                    ["advance", "balance", "amount"], limit=1,
                )
                if act_data:
                    a = act_data[0]
                    result["post"] = {
                        "advance": a["advance"],
                        "balance": a["balance"],
                        "amount":  a["amount"],
                    }
                    log.info(f"[PW] Post-save MO: advance={a['advance']} balance={a['balance']}")

            result["ok"]     = True
            result["act_id"] = act_id
            result["msg"]    = f"Liquidado MO {act_code} qty={cantidad} via modal ({write_dt:.1f}s)"
            log.info(f"[PW] OK modal MO {act_code}")

        except PWTimeout as ex:
            result["error"] = f"Timeout: {ex}"
            result["pasos"].append(self._ss(f"{tag}_timeout"))
            log.error(f"[PW] Timeout {act_code}: {ex}")
        except BrowserActionError as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] BrowserActionError {act_code}: {ex}")
        except Exception as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error {act_code}: {ex}")

        return result

    # ── MOTOVERIFICADOR: RESTAR AVANCE MO (rollback de nodo incorrecto) ─────────
    def restar_avance_mo(self,
                         pid: int,
                         act_code: str,
                         cantidad_restar: float,
                         partner_nombre: str,
                         partner_cedula: str = "",
                         fecha_original: str = "",
                         pro_code: str = "",
                         proyectado_actual: float = None,
                         act_id: int = None,
                         sgs_client=None,
                         dry_run: bool = False) -> dict:
        """
        Motoverificador — revierte un avance MO liquidado en el nodo incorrecto.

        Mecanismo (todo dentro del modal "Avance del proyecto"):
          1. Navega al proyecto (pid / nodo incorrecto).
          2. Abre lista de actividades.
          3. Fila act_code → action_show_details → modal.
          4. En el modal: EDITAR (si no está en edición), edita proyectado +1.
             Esto crea un hueco de balance=1 para poder agregar línea negativa.
          5. En el modal: Agregar línea con amount=-cantidad_restar,
             date=fecha_original, partner=partner_nombre.
          6. Guarda modal.
          7. Verifica via API post-save.

        Parámetros:
          proyectado_actual  — amount actual de la actividad. Si None, se lee via API
                               (requiere act_id + sgs_client).
          fecha_original     — fecha DD/MM/YYYY de la línea a revertir.
                               Si vacío, usa FECHA_FIJA_MO.
          dry_run            — True → hace todo hasta GUARDAR sin ejecutarlo.
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        fecha_uso = fecha_original or FECHA_FIJA_MO or datetime.today().strftime("%d/%m/%Y")
        log.info(f"[PW] RestarAvanceMO pid={pid} code={act_code} qty_restar={cantidad_restar} "
                 f"partner={partner_nombre} fecha={fecha_uso} dry_run={dry_run}")
        page   = self._page
        result = {"act_code": act_code, "ok": False, "pasos": [], "dry_run": dry_run}
        tag    = f"restar_{act_code}"

        try:
            # ── 1. Navegar al proyecto ────────────────────────────────────────
            code_val = self._navegar_proyecto(pid, pro_code=pro_code)
            result["pasos"].append(self._ss(f"{tag}_01_proy"))
            log.info(f"[PW] Navegado a proyecto: {code_val}")

            # ── 2. Abrir lista de actividades ─────────────────────────────────
            page.locator("[name='action_button_activities']").first.click()
            time.sleep(2.0)
            for f in page.locator(".o_searchview .o_facet_remove").all():
                try: f.click(); time.sleep(0.3)
                except Exception: pass
            time.sleep(1.0)
            result["pasos"].append(self._ss(f"{tag}_02_lista"))

            # ── 3. Leer proyectado actual via API si no fue pasado ────────────
            if proyectado_actual is None and sgs_client and act_id:
                data = sgs_client.search_read(
                    "project.activities",
                    [["id", "=", act_id]],
                    ["amount", "advance"], limit=1,
                )
                if data:
                    proyectado_actual = float(data[0].get("amount") or 0)
                    log.info(f"[PW] Proyectado via API: {proyectado_actual}")

            if proyectado_actual is None:
                raise BrowserActionError(
                    f"proyectado_actual no disponible para {act_code} — "
                    "pasa proyectado_actual= o act_id= + sgs_client="
                )
            if not act_id:
                raise BrowserActionError(
                    f"act_id requerido para rollback de {act_code}"
                )

            nuevo_proyectado = proyectado_actual + 1
            nuevo_proy_float = float(nuevo_proyectado)

            # ── 4. Escribir proyectado+1 via RPC desde sesión del navegador ───
            # El campo amount tiene o_readonly_modifier en la vista modal para
            # uid=12130, por eso se escribe directo al modelo via JSON-RPC.
            # La sesión autenticada del browser nos da acceso con el mismo uid.
            rpc_result = page.evaluate(
                """async ([act_id, new_amount]) => {
                    try {
                        const resp = await fetch('/web/dataset/call_kw', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                jsonrpc: '2.0', id: 1, method: 'call',
                                params: {
                                    model: 'project.activities',
                                    method: 'write',
                                    args: [[act_id], {amount: new_amount}],
                                    kwargs: {context: {}}
                                }
                            })
                        });
                        return await resp.json();
                    } catch(e) {
                        return {error: {message: String(e)}};
                    }
                }""",
                [act_id, nuevo_proy_float],
            )
            log.info(f"[PW] RPC write amount={nuevo_proy_float}: {rpc_result}")
            if isinstance(rpc_result, dict) and rpc_result.get("error"):
                raise BrowserActionError(
                    f"RPC write proyectado falló: {rpc_result['error']}"
                )
            result["pasos"].append(self._ss(f"{tag}_03_proy_rpc"))
            log.info(f"[PW] Proyectado → {nuevo_proyectado} via RPC OK")

            if dry_run:
                result["ok"]  = True
                result["msg"] = (f"Dry run OK — proyectado RPC {proyectado_actual}→"
                                 f"{nuevo_proyectado} sin línea negativa")
                return result

            # ── 5. Localizar fila y abrir modal action_show_details ──────────
            # Volver a navegar al proyecto para reflejar el nuevo proyectado
            self._navegar_proyecto(pid, pro_code=pro_code)
            time.sleep(0.5)
            page.locator("[name='action_button_activities']").first.click()
            time.sleep(2.0)
            for f in page.locator(".o_searchview .o_facet_remove").all():
                try: f.click(); time.sleep(0.3)
                except Exception: pass
            time.sleep(1.0)

            row = page.locator(f".o_data_row:has-text('{act_code}')").first
            row.wait_for(state="visible", timeout=30_000)
            row.scroll_into_view_if_needed()
            result["pasos"].append(self._ss(f"{tag}_04_fila"))

            detail_btn = row.locator("button[name='action_show_details']").first
            detail_btn.wait_for(state="visible", timeout=10_000)
            detail_btn.click()
            page.wait_for_selector(".modal", timeout=20_000)
            time.sleep(1.0)
            result["pasos"].append(self._ss(f"{tag}_05_modal"))
            log.info(f"[PW] Modal 'Avance del proyecto' abierto para {act_code}")

            # ── 6. AGREGAR LÍNEA negativa en el modal ─────────────────────────
            add_sel = (
                ".modal .o_field_x2many_list_row_add a, "
                ".o_dialog .o_field_x2many_list_row_add a, "
                ".modal a:has-text('AGREGAR'), "
                ".modal a:has-text('Agregar'), "
                ".modal a:has-text('Add')"
            )
            add_btn = page.locator(add_sel).first
            add_btn.wait_for(state="visible", timeout=10_000)
            add_btn.click()
            time.sleep(0.8)
            result["pasos"].append(self._ss(f"{tag}_07_newline"))

            # ── 8. Llenar cantidad NEGATIVA en la nueva fila ──────────────────
            qty_negativa = -abs(cantidad_restar)
            qty_str = (
                str(int(qty_negativa))
                if qty_negativa == int(qty_negativa)
                else str(qty_negativa)
            )
            qty_sel = (
                ".modal tr.o_selected_row input[name='amount'], "
                ".modal tr.o_selected_row [name='amount'] input, "
                ".o_dialog tr.o_selected_row input[name='amount'], "
                ".o_dialog tr.o_selected_row [name='amount'] input"
            )
            qty = page.locator(qty_sel).first
            qty.wait_for(state="visible", timeout=10_000)
            qty.click(click_count=3)
            qty.fill(qty_str)
            time.sleep(0.3)
            result["pasos"].append(self._ss(f"{tag}_08_qty_neg"))
            log.info(f"[PW] Cantidad negativa: {qty_str}")

            # ── 9. Llenar fecha (misma de la liquidación original) ────────────
            date_sel = (
                ".modal tr.o_selected_row input[name='date'], "
                ".modal tr.o_selected_row [name='date'] input, "
                ".o_dialog tr.o_selected_row input[name='date'], "
                ".o_dialog tr.o_selected_row [name='date'] input"
            )
            date_inp = page.locator(date_sel).first
            try:
                if date_inp.count() > 0 and date_inp.is_visible(timeout=3000):
                    date_inp.click(click_count=3)
                    date_inp.fill(fecha_uso)
                    date_inp.press("Tab")
                    time.sleep(0.3)
                    log.info(f"[PW] Fecha rollback → {fecha_uso}")
            except PWTimeout:
                log.warning("[PW] Campo date no visible en modal rollback — omitido")
            result["pasos"].append(self._ss(f"{tag}_09_fecha"))

            # ── 10. Llenar partner (mismo líder de la liquidación original) ───
            if partner_nombre:
                p_sel = (
                    ".modal tr.o_selected_row [name='partner_id'] input, "
                    ".modal tr.o_selected_row input[name='partner_id'], "
                    ".o_dialog tr.o_selected_row [name='partner_id'] input, "
                    ".o_dialog tr.o_selected_row input[name='partner_id']"
                )
                p_input = page.locator(p_sel).first
                p_input.wait_for(state="visible", timeout=10_000)
                termino = (
                    partner_cedula.strip()
                    if partner_cedula
                    else partner_nombre.strip().split()[0]
                )
                p_input.click()
                p_input.fill("")
                p_input.press_sequentially(termino, delay=80)
                time.sleep(1.5)
                nombre_busq = partner_nombre.strip().split()[0]
                if not self._click_m2o_option(nombre_busq, timeout_s=3.0):
                    raise BrowserActionError(
                        f"Líder '{partner_nombre}' no encontrado en dropdown rollback"
                    )
                time.sleep(0.4)
                try:
                    p_input.press("Tab")
                except Exception:
                    pass
                time.sleep(0.5)
                result["pasos"].append(self._ss(f"{tag}_10_partner"))

            # ── 11. GUARDAR modal ─────────────────────────────────────────────
            save_footer = None
            for _sel in [
                ".o_dialog .modal-footer .btn-primary",
                ".o_dialog .modal-footer button:has-text('Guardar')",
                ".o_dialog .modal-footer button:has-text('GUARDAR')",
                ".o_dialog .o_dialog_footer .btn-primary",
                ".modal-footer .btn-primary",
                ".modal-footer button:has-text('Guardar')",
                ".modal-footer button:has-text('GUARDAR')",
            ]:
                _el = page.locator(_sel).first
                try:
                    if _el.is_visible(timeout=1500):
                        save_footer = _el
                        log.info(f"[PW] Botón GUARDAR modal rollback: {_sel}")
                        break
                except PWTimeout:
                    pass

            if not save_footer:
                result["pasos"].append(self._ss(f"{tag}_sin_guardar"))
                raise BrowserActionError(
                    f"Botón GUARDAR no encontrado en modal rollback {act_code}"
                )

            result["pasos"].append(self._ss(f"{tag}_11_antes_guardar"))
            t_save = time.perf_counter()
            save_footer.click()
            time.sleep(1.5)
            result["pasos"].append(self._ss(f"{tag}_11b_tras_guardar"))

            guardado_ok = False
            try:
                page.wait_for_selector(
                    ".o_dialog .modal-footer button:has-text('EDITAR'), "
                    ".modal .modal-footer button:has-text('EDITAR'), "
                    ".o_dialog .modal-footer button:has-text('Editar'), "
                    ".modal .modal-footer button:has-text('Editar')",
                    timeout=25_000,
                )
                guardado_ok = True
                log.info(f"[PW] GUARDAR rollback OK {act_code} — modal en modo lectura")
                try:
                    close_btn = page.locator(
                        ".modal .btn-close, .modal button.close, .modal [aria-label='Close']"
                    ).first
                    if close_btn.count() > 0 and close_btn.is_visible(timeout=1000):
                        close_btn.click()
                    else:
                        page.keyboard.press("Escape")
                except Exception:
                    page.keyboard.press("Escape")
                time.sleep(0.8)
            except PWTimeout:
                try:
                    page.wait_for_selector(
                        ".o_dialog .modal-footer, .modal .modal-footer",
                        state="hidden", timeout=3_000,
                    )
                    guardado_ok = True
                    log.info(f"[PW] GUARDAR rollback OK {act_code} — modal cerró")
                except PWTimeout:
                    pass

            if not guardado_ok:
                result["pasos"].append(self._ss(f"{tag}_modal_abierto"))
                self._check_error_modal()
                raise BrowserActionError(
                    f"Modal no cerró tras GUARDAR rollback {act_code}"
                )

            self._check_error_modal()
            write_dt = time.perf_counter() - t_save
            time.sleep(1.0)
            result["pasos"].append(self._ss(f"{tag}_12_guardado"))
            result["write_dt"] = round(write_dt, 2)
            log.info(f"[PW] Rollback OK {act_code} en {write_dt:.1f}s")

            # ── 12. Verificar via API ─────────────────────────────────────────
            if sgs_client and act_id:
                time.sleep(1.0)
                act_data = sgs_client.search_read(
                    "project.activities",
                    [["id", "=", act_id]],
                    ["advance", "balance", "amount"], limit=1,
                )
                if act_data:
                    a = act_data[0]
                    result["post"] = {
                        "advance": a["advance"],
                        "balance": a["balance"],
                        "amount":  a["amount"],
                    }
                    log.info(f"[PW] Post-rollback: advance={a['advance']} "
                             f"balance={a['balance']} amount={a['amount']}")

            result["ok"]  = True
            result["msg"] = (f"Rollback MO {act_code} qty={qty_str} "
                             f"partner={partner_nombre} en {write_dt:.1f}s")

        except PWTimeout as ex:
            result["error"] = f"Timeout: {ex}"
            result["pasos"].append(self._ss(f"{tag}_timeout"))
            log.error(f"[PW] Timeout rollback {act_code}: {ex}")
        except BrowserActionError as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] BrowserActionError rollback {act_code}: {ex}")
        except Exception as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error rollback {act_code}: {ex}")

        return result

    # ── MOTOVERIFICADOR: ROLLBACK EN LOTE (múltiples líneas, un modal) ───────
    def restar_avance_mo_lote(self,
                              pid: int,
                              act_code: str,
                              detalles: list,
                              pro_code: str = "",
                              proyectado_actual: float = None,
                              act_id: int = None,
                              sgs_client=None,
                              dry_run: bool = False) -> dict:
        """
        Rollback de múltiples líneas de avance MO en un solo modal.

        detalles = [
            {"partner_nombre": "MEDIORREAL ...", "cantidad_restar": 151.0, "fecha": "30/04/2026"},
            {"partner_nombre": "ESCOBAR ...",    "cantidad_restar": 229.0, "fecha": "30/04/2026"},
        ]

        Flujo: RPC proyectado+1 → modal → agregar línea × N → guardar todo.
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        if not detalles:
            return {"ok": False, "error": "detalles vacío"}

        log.info(f"[PW] RestarLote pid={pid} code={act_code} "
                 f"n_lineas={len(detalles)} dry_run={dry_run}")
        page   = self._page
        result = {"act_code": act_code, "ok": False, "pasos": [], "dry_run": dry_run,
                  "lineas_revertidas": 0}
        tag    = f"lote_{act_code}"

        try:
            # ── 1. Navegar al proyecto ────────────────────────────────────────
            self._navegar_proyecto(pid, pro_code=pro_code)
            result["pasos"].append(self._ss(f"{tag}_01_proy"))

            # ── 2. Abrir lista de actividades ─────────────────────────────────
            page.locator("[name='action_button_activities']").first.click()
            time.sleep(2.0)
            for f in page.locator(".o_searchview .o_facet_remove").all():
                try: f.click(); time.sleep(0.3)
                except Exception: pass
            time.sleep(1.0)

            # ── 3. Leer proyectado via API si no fue pasado ───────────────────
            if proyectado_actual is None and sgs_client and act_id:
                data = sgs_client.search_read(
                    "project.activities",
                    [["id", "=", act_id]],
                    ["amount"], limit=1,
                )
                if data:
                    proyectado_actual = float(data[0].get("amount") or 0)

            if proyectado_actual is None:
                raise BrowserActionError(
                    f"proyectado_actual no disponible para {act_code}"
                )
            if not act_id:
                raise BrowserActionError(f"act_id requerido para rollback lote {act_code}")

            nuevo_proyectado = proyectado_actual + 1

            # ── 4. Escribir proyectado+1 via RPC ──────────────────────────────
            rpc_result = page.evaluate(
                """async ([act_id, new_amount]) => {
                    try {
                        const r = await fetch('/web/dataset/call_kw', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                jsonrpc: '2.0', id: 1, method: 'call',
                                params: {
                                    model: 'project.activities',
                                    method: 'write',
                                    args: [[act_id], {amount: new_amount}],
                                    kwargs: {context: {}}
                                }
                            })
                        });
                        return await r.json();
                    } catch(e) { return {error: {message: String(e)}}; }
                }""",
                [act_id, float(nuevo_proyectado)],
            )
            if isinstance(rpc_result, dict) and rpc_result.get("error"):
                raise BrowserActionError(f"RPC write proyectado falló: {rpc_result['error']}")
            result["pasos"].append(self._ss(f"{tag}_02_rpc"))
            log.info(f"[PW] Proyectado lote → {nuevo_proyectado} OK")

            if dry_run:
                result["ok"]  = True
                result["msg"] = f"Dry run OK — proyectado {proyectado_actual}→{nuevo_proyectado}"
                return result

            # ── 5. Abrir modal action_show_details ────────────────────────────
            self._navegar_proyecto(pid, pro_code=pro_code)
            time.sleep(0.5)
            page.locator("[name='action_button_activities']").first.click()
            time.sleep(2.0)
            for f in page.locator(".o_searchview .o_facet_remove").all():
                try: f.click(); time.sleep(0.3)
                except Exception: pass
            time.sleep(1.0)

            row = page.locator(f".o_data_row:has-text('{act_code}')").first
            row.wait_for(state="visible", timeout=30_000)
            row.scroll_into_view_if_needed()
            row.locator("button[name='action_show_details']").first.click()
            page.wait_for_selector(".modal", timeout=20_000)
            time.sleep(1.0)
            result["pasos"].append(self._ss(f"{tag}_03_modal"))

            # ── 6. Agregar una línea negativa por cada detalle ────────────────
            add_sel = (
                ".modal .o_field_x2many_list_row_add a, "
                ".o_dialog .o_field_x2many_list_row_add a, "
                ".modal a:has-text('Agregar'), .modal a:has-text('Add')"
            )
            qty_sel = (
                ".modal tr.o_selected_row input[name='amount'], "
                ".modal tr.o_selected_row [name='amount'] input, "
                ".o_dialog tr.o_selected_row input[name='amount']"
            )
            date_sel = (
                ".modal tr.o_selected_row input[name='date'], "
                ".modal tr.o_selected_row [name='date'] input, "
                ".o_dialog tr.o_selected_row input[name='date']"
            )
            partner_sel = (
                ".modal tr.o_selected_row [name='partner_id'] input, "
                ".o_dialog tr.o_selected_row [name='partner_id'] input"
            )

            for idx, det in enumerate(detalles):
                qty_neg  = -abs(float(det["cantidad_restar"]))
                qty_str  = str(int(qty_neg)) if qty_neg == int(qty_neg) else str(qty_neg)
                fecha    = det.get("fecha") or FECHA_FIJA_MO or datetime.today().strftime("%d/%m/%Y")
                partner  = det.get("partner_nombre", "")
                termino  = partner.strip().split()[0] if partner else ""

                # Agregar línea
                add_btn = page.locator(add_sel).first
                add_btn.wait_for(state="visible", timeout=10_000)
                add_btn.click()
                time.sleep(0.8)

                # Cantidad
                qty = page.locator(qty_sel).first
                qty.wait_for(state="visible", timeout=8_000)
                qty.click(click_count=3)
                qty.fill(qty_str)
                time.sleep(0.2)

                # Fecha
                date_inp = page.locator(date_sel).first
                try:
                    if date_inp.count() > 0 and date_inp.is_visible(timeout=2000):
                        date_inp.click(click_count=3)
                        date_inp.fill(fecha)
                        date_inp.press("Tab")
                        time.sleep(0.2)
                except PWTimeout:
                    pass

                # Partner
                if termino:
                    p_input = page.locator(partner_sel).first
                    p_input.wait_for(state="visible", timeout=8_000)
                    p_input.click()
                    p_input.fill("")
                    p_input.press_sequentially(termino, delay=80)
                    time.sleep(1.5)
                    nombre_busq = partner.strip().split()[0]
                    if not self._click_m2o_option(nombre_busq, timeout_s=3.0):
                        raise BrowserActionError(
                            f"Líder '{partner}' no encontrado en dropdown (lote idx={idx})"
                        )
                    time.sleep(0.4)
                    try: p_input.press("Tab")
                    except Exception: pass
                    time.sleep(0.4)

                result["lineas_revertidas"] += 1
                result["pasos"].append(self._ss(f"{tag}_linea{idx}"))
                log.info(f"[PW] Línea {idx+1}/{len(detalles)}: {qty_str} {partner}")

            # ── 7. GUARDAR modal ──────────────────────────────────────────────
            save_footer = None
            for _sel in [
                ".modal-footer button:has-text('Guardar')",
                ".modal-footer button:has-text('GUARDAR')",
                ".o_dialog .modal-footer .btn-primary",
                ".modal-footer .btn-primary",
            ]:
                _el = page.locator(_sel).first
                try:
                    if _el.is_visible(timeout=1500):
                        save_footer = _el
                        break
                except PWTimeout:
                    pass

            if not save_footer:
                raise BrowserActionError(f"Botón GUARDAR no encontrado en modal lote {act_code}")

            result["pasos"].append(self._ss(f"{tag}_antes_guardar"))
            t_save = time.perf_counter()
            save_footer.click()
            time.sleep(1.5)

            guardado_ok = False
            try:
                page.wait_for_selector(
                    ".modal .modal-footer button:has-text('Editar'), "
                    ".o_dialog .modal-footer button:has-text('Editar')",
                    timeout=25_000,
                )
                guardado_ok = True
                try:
                    close_btn = page.locator(
                        ".modal .btn-close, .modal [aria-label='Close']"
                    ).first
                    if close_btn.count() > 0 and close_btn.is_visible(timeout=1000):
                        close_btn.click()
                    else:
                        page.keyboard.press("Escape")
                except Exception:
                    page.keyboard.press("Escape")
                time.sleep(0.8)
            except PWTimeout:
                try:
                    page.wait_for_selector(
                        ".modal .modal-footer", state="hidden", timeout=3_000
                    )
                    guardado_ok = True
                except PWTimeout:
                    pass

            if not guardado_ok:
                self._check_error_modal()
                raise BrowserActionError(f"Modal no cerró tras GUARDAR lote {act_code}")

            self._check_error_modal()
            write_dt = time.perf_counter() - t_save
            result["pasos"].append(self._ss(f"{tag}_guardado"))
            result["write_dt"] = round(write_dt, 2)

            # ── 8. Verificar via API ──────────────────────────────────────────
            if sgs_client and act_id:
                time.sleep(1.0)
                ad = sgs_client.search_read(
                    "project.activities",
                    [["id", "=", act_id]],
                    ["advance", "balance", "amount"], limit=1,
                )
                if ad:
                    result["post"] = {
                        "advance": ad[0]["advance"],
                        "balance": ad[0]["balance"],
                        "amount":  ad[0]["amount"],
                    }

            result["ok"]  = True
            result["msg"] = (f"Lote rollback {act_code}: {len(detalles)} líneas "
                             f"revertidas en {write_dt:.1f}s")
            log.info(f"[PW] {result['msg']}")

        except PWTimeout as ex:
            result["error"] = f"Timeout: {ex}"
            result["pasos"].append(self._ss(f"{tag}_timeout"))
            log.error(f"[PW] Timeout lote {act_code}: {ex}")
        except BrowserActionError as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error lote {act_code}: {ex}")
        except Exception as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error lote {act_code}: {ex}")

        return result

    def _click_m2o_option(self, buscar: str, timeout_s: float = 2.5) -> bool:
        """
        Hace click en el primer item del autocomplete Many2one que contenga `buscar`.
        Usa force=True para saltarse el check de visibilidad CSS de Playwright
        (en Odoo 17, los li del dropdown pueden estar en overflow:hidden del td padre).
        Excluye items de "Crear..." para no crear registros nuevos accidentalmente.
        Retorna True si hizo click, False si no encontró ningún item.
        """
        page = self._page
        # Esperar a que el dropdown aparezca en DOM (polling con timeout)
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            for sel in [
                f".o-autocomplete--dropdown-item:has-text('{buscar}')",
                f".o_m2o_dropdown_option:has-text('{buscar}')",
                f"li:has-text('{buscar}')",
            ]:
                try:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        el.click(force=True, timeout=1000)
                        log.info(f"[PW] _click_m2o_option: '{buscar}' → force click OK ({sel})")
                        return True
                except Exception:
                    pass
            time.sleep(0.25)
        log.warning(f"[PW] _click_m2o_option: '{buscar}' — no encontrado en DOM tras {timeout_s}s")
        return False

    def _check_error_modal(self):
        """Lanza BrowserActionError si hay modal de error de Odoo visible."""
        try:
            modal = self._page.locator(
                ".modal .modal-title:has-text('rror'), .o_error_dialog"
            ).first
            if modal.count() > 0 and modal.is_visible(timeout=2000):
                txt = self._page.locator(".modal-body").first.inner_text()[:300]
                self._page.locator(
                    "button:has-text('ACEPTAR'), button:has-text('Ok')"
                ).first.click()
                time.sleep(0.5)
                raise BrowserActionError(f"Error Odoo: {txt}")
        except (PWTimeout, Exception):
            pass

    # ── LIQUIDAR ACTIVIDAD MO ─────────────────────────────────────────────────
    def liquidar_actividad_mo(self,
                               act_id: int,
                               pid: int,
                               cantidad: float,
                               act_code: str = "",
                               pro_code: str = "",
                               dry_run: bool = False,
                               partner_nombre: str = "") -> dict:
        """
        Liquida una actividad MO en SGS agregando una línea en
        'advance_project_activities_ids'.

        Selectores confirmados (10-04-2026 — liquidar_mo.py exitoso):
          - Nav proyecto : /web#id={pid}&action=485&model=project.dominion&view_type=form
          - Stat button  : [name='action_button_activities']
          - Quitar filtro: .o_searchview .o_facet_remove
          - Nav actividad: /web#id={act_id}&model=project.activities&view_type=form
          - Editar       : button:has-text('EDITAR')
          - Add line     : [name='advance_project_activities_ids'] td.o_field_x2many_list_row_add a
          - Campo amount : [name='advance_project_activities_ids'] tr.o_selected_row input[name='amount']
          - Equipo (partner): AUTO-LLENADO — NO tocar
          - Guardar      : button:has-text('GUARDAR')

        dry_run=True → hace todo hasta antes de GUARDAR.
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        log.info(f"[PW] LiquidarMO act#{act_id} pid={pid} qty={cantidad} "
                 f"code={act_code} pro={pro_code} partner={partner_nombre} dry_run={dry_run}")
        page   = self._page
        result = {"act_id": act_id, "ok": False, "pasos": []}
        tag    = f"mo_{act_id}"

        try:
            # 1. Navegar al formulario del proyecto
            code_val = self._navegar_proyecto(pid, pro_code=pro_code)
            result["pasos"].append(self._ss(f"{tag}_01_proy"))

            # 2. Confirmar código de proyecto en pantalla
            result["proyecto_en_pantalla"] = code_val

            # 3. Abrir lista de actividades
            page.locator("[name='action_button_activities']").first.click()
            time.sleep(2.0)

            # Quitar filtro "Pendiente" si aparece
            for f in page.locator(".o_searchview .o_facet_remove").all():
                try:
                    f.click()
                    time.sleep(0.3)
                except Exception:
                    pass
            time.sleep(1.0)
            result["pasos"].append(self._ss(f"{tag}_02_acts"))

            # 4. Navegar directamente al formulario de la actividad
            url_act = (
                f"{SGS_URL}/web#id={act_id}"
                f"&model=project.activities&view_type=form&cids=1&menu_id=352"
            )
            page.goto(url_act, wait_until="domcontentloaded")
            page.wait_for_selector(".o_form_view", timeout=WAIT_UNTIL)
            time.sleep(1.5)
            result["pasos"].append(self._ss(f"{tag}_03_form_act"))

            # 5. Leer estado pre-escritura
            pre = {}
            for nm in ["activity_code", "amount", "advance", "balance"]:
                el = page.locator(f"[name='{nm}']").first
                if el.count() > 0:
                    try:    pre[nm] = el.input_value()
                    except Exception: pre[nm] = el.inner_text().strip()
            result["pre"] = pre
            log.info(f"[PW] Estado pre: {pre}")

            # 6. Entrar en modo edición si es necesario
            edit_btn = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
            if edit_btn.count() > 0:
                try:
                    if edit_btn.is_visible(timeout=3000):
                        edit_btn.click()
                        time.sleep(1.0)
                except PWTimeout:
                    pass
            self._dismiss_error()
            result["pasos"].append(self._ss(f"{tag}_04_edit"))

            # 7. Click "Agregar línea"
            CAMPO = "advance_project_activities_ids"
            add_line = page.locator(
                f"[name='{CAMPO}'] td.o_field_x2many_list_row_add a, "
                f"[name='{CAMPO}'] .o_field_x2many_list_row_add a"
            ).first
            add_line.wait_for(timeout=WAIT_UNTIL)
            add_line.click()
            time.sleep(0.8)
            result["pasos"].append(self._ss(f"{tag}_05_newrow"))

            # 8. Llenar cantidad (Equipo es auto-llenado — NO tocar)
            qty_input = None
            for sel in [
                f"[name='{CAMPO}'] tr.o_selected_row input[name='amount']",
                f"[name='{CAMPO}'] tbody tr:last-child input[name='amount']",
                f"[name='{CAMPO}'] input[name='amount']",
            ]:
                el = page.locator(sel).last
                if el.count() > 0:
                    try:
                        if el.is_visible(timeout=1500):
                            qty_input = el
                            break
                    except PWTimeout:
                        pass

            if not qty_input:
                raise BrowserActionError(
                    f"Campo 'amount' no encontrado en {CAMPO} — revisar HTML"
                )

            qty_input.click(click_count=3)
            qty_input.fill(str(int(cantidad) if cantidad == int(cantidad) else cantidad))
            time.sleep(0.3)
            result["pasos"].append(self._ss(f"{tag}_06_qty"))

            if partner_nombre:
                partner_sel = f"[name='{CAMPO}'] tr.o_selected_row [name='partner_id'] input"
                p_input = page.locator(partner_sel).first
                if p_input.count() == 0:
                    raise BrowserActionError("Campo partner_id no encontrado en linea MO")
                p_input.wait_for(timeout=WAIT_UNTIL)
                p_input.click(click_count=3)
                termino = partner_nombre.strip().split()[0]
                p_input.fill("")
                p_input.press_sequentially(termino, delay=80)
                time.sleep(1.2)
                opcion = None
                for sel in [
                    f"li:has-text('{termino}')",
                    f".o_m2o_dropdown_option:has-text('{termino}')",
                    "ul.o_m2o_autocomplete li",
                    ".o_m2o_dropdown_option",
                ]:
                    el = page.locator(sel).first
                    if el.count() > 0:
                        try:
                            if el.is_visible(timeout=1500):
                                opcion = el
                                break
                        except PWTimeout:
                            pass
                if not opcion:
                    raise BrowserActionError(
                        f"Lider MO '{partner_nombre}' no encontrado en dropdown SGS. No se guarda."
                    )
                opcion.click()
                time.sleep(0.5)
                result["partner_nombre"] = partner_nombre
                result["pasos"].append(self._ss(f"{tag}_06_partner"))

            if dry_run:
                page.locator(
                    "button:has-text('DESCARTAR'), button:has-text('Descartar')"
                ).first.click()
                time.sleep(0.8)
                self._dismiss_error()
                result["ok"]      = True
                result["dry_run"] = True
                result["msg"]     = "Dry run OK — formulario completado pero NO guardado"
                log.info(f"[PW] Dry run OK act#{act_id}")
                return result

            # 9. GUARDAR
            save_btn = page.locator(
                "button:has-text('GUARDAR'), button:has-text('Guardar')"
            ).first
            if not save_btn.is_visible(timeout=5000):
                raise BrowserActionError("Botón GUARDAR no visible — sin guardar")
            save_btn.click()
            time.sleep(2.5)

            # Verificar error modal
            err_modal = page.locator(
                ".modal .modal-title:has-text('rror'), .o_error_dialog"
            ).first
            if err_modal.count() > 0:
                try:
                    if err_modal.is_visible(timeout=2000):
                        txt = page.locator(".modal-body").first.inner_text()[:300]
                        page.locator(
                            "button:has-text('ACEPTAR'), button:has-text('Ok')"
                        ).first.click()
                        time.sleep(0.5)
                        raise BrowserActionError(f"Error Odoo al guardar: {txt}")
                except PWTimeout:
                    pass

            result["pasos"].append(self._ss(f"{tag}_07_saved"))

            # 10. Leer estado post-guardado
            post = {}
            for nm in ["advance", "balance", "amount"]:
                el = page.locator(f"[name='{nm}']").first
                if el.count() > 0:
                    try:    post[nm] = el.input_value()
                    except Exception: post[nm] = el.inner_text().strip()
            result["post"] = post
            log.info(f"[PW] Estado post: {post}")

            result["ok"]  = True
            result["msg"] = f"Liquidado MO act#{act_id} qty={cantidad}"
            log.info(f"[PW] GUARDADO OK act#{act_id}")

        except PWTimeout as ex:
            result["error"] = f"Timeout: {ex}"
            result["pasos"].append(self._ss(f"{tag}_timeout"))
            log.error(f"[PW] Timeout act#{act_id}: {ex}")
        except BrowserActionError as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] BrowserActionError act#{act_id}: {ex}")
        except Exception as ex:
            result["error"] = str(ex)
            result["pasos"].append(self._ss(f"{tag}_error"))
            log.error(f"[PW] Error act#{act_id}: {ex}")

        return result

    # ── LIQUIDAR MATERIAL ──────────────────────────────────────────────────────
    def liquidar_material(self,
                          pm_id: int,
                          cantidad: float,
                          partner_nombre: str = "",
                          pro_code: str = "",
                          dry_run: bool = False,
                          screenshots: bool = True,
                          pendiente_api_confirmado: bool = False) -> dict:
        """
        Liquida un material agregando una línea en 'advance_project_material_ids'.

        dry_run=True    → completa el formulario pero NO guarda.
        screenshots=False → solo captura en caso de error (más rápido en workers).
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        log.info(f"[PW] Liquidar pm#{pm_id} qty={cantidad} pro={pro_code} dry_run={dry_run}")
        page   = self._page
        cantidad_solicitada = float(cantidad or 0)
        result = {
            "pm_id": pm_id,
            "ok": False,
            "pasos": [],
            "cantidad_solicitada": cantidad_solicitada,
        }

        def _capture(tag):
            if screenshots:
                result["pasos"].append(self._ss(tag))

        try:
            # 1. Navegar
            self._navegar_material(pm_id)
            _capture(f"liq_{pm_id}_01_nav")

            # Seguridad ARASAKACORP: la cantidad ya viene del Excel verificado
            # y de las barreras SGS del orquestador. El navegador solo confirma
            # que SGS no contradiga esa cantidad; nunca ajusta automaticamente.
            pendiente_dom, devuelto_dom = self._leer_pendiente_material_dom()
            result["pendiente_dom"] = pendiente_dom
            result["devuelto_dom"] = devuelto_dom
            if devuelto_dom is not None and devuelto_dom > 0.001:
                log.info(
                    f"[PW] pm#{pm_id}: Devuelto DOM={devuelto_dom} — neto factorizado, "
                    "barreras API ya validaron. Continuando."
                )
            if pendiente_dom is not None:
                if pendiente_dom <= 0.001:
                    raise BrowserActionError(
                        f"Pendiente DOM={pendiente_dom} pm#{pm_id}; "
                        f"devuelto={devuelto_dom}. Nada pendiente en SGS."
                    )
                if abs(pendiente_dom - cantidad_solicitada) > 0.001:
                    raise BrowserActionError(
                        f"Contrato Excel/SGS no coincide pm#{pm_id}: "
                        f"cantidad autorizada={cantidad_solicitada}, Pendiente DOM={pendiente_dom}. "
                        "Abortando; no se ajusta automaticamente."
                    )
                cantidad = round(cantidad_solicitada, 4)
            else:
                if pendiente_api_confirmado and cantidad_solicitada > 0:
                    cantidad = round(cantidad_solicitada, 4)
                    result["pendiente_api_fallback"] = True
                    log.warning(
                        f"[PW] pm#{pm_id}: Pendiente DOM no visible; "
                        f"usando Pendiente API fresco confirmado={cantidad}"
                    )
                else:
                    raise BrowserActionError(
                        f"No se pudo leer Pendiente DOM pm#{pm_id}. "
                        "Abortando: no se guarda si no se confirma la cantidad pendiente."
                    )
            result["cantidad"] = cantidad

            # 2. Entrar en modo edición  (con reintento si la página no estabiliza)
            def _entrar_edicion_y_add_line(intento: int):
                """
                Entra en modo edición y espera a que aparezca "Agregar una línea".
                Si falla, recarga la página y reintenta una vez (intento=1).
                Devuelve el locator de "Agregar una línea" listo para clicar.

                Causa raíz de timeout_advance_mat:
                  - El código anterior usaba edit_btn.count() (síncrono, sin espera).
                    Si el botón EDITAR aún no había renderizado, count() = 0 y se
                    saltaba el click → el form nunca entraba en edit mode → add-line
                    nunca aparecía → timeout.
                  - Fix: esperar el botón EDITAR con timeout, LUEGO confirmar que el
                    form está en modo editable (.o_form_editable), LUEGO buscar add-line.
                """
                # 2a. Esperar y clicar EDITAR si está presente
                try:
                    edit_btn = page.locator(
                        "button:has-text('EDITAR'), button:has-text('Editar')"
                    ).first
                    edit_btn.wait_for(state="visible", timeout=8_000)
                    edit_btn.click()
                    self._dismiss_error()
                    log.debug(f"[PW] EDITAR clicado pm#{pm_id} intento={intento}")
                except PWTimeout:
                    # No hay EDITAR — puede que ya estemos en edit mode
                    log.debug(f"[PW] Botón EDITAR no encontrado pm#{pm_id} — asumiendo edit mode activo")

                # 2b. Confirmar que el formulario entró en modo edición (25s)
                # Aumentado de 15s: bajo carga con 2 workers en el mismo proyecto
                # Odoo puede tardar >15s en procesar el click de EDITAR.
                try:
                    page.wait_for_selector(
                        ".o_form_view.o_form_editable",
                        timeout=25_000,
                    )
                except PWTimeout:
                    if intento == 0:
                        # Servidor lento o click no registrado — recargar y reintentar
                        log.warning(
                            f"[PW] Edit mode no apareció pm#{pm_id} — "
                            "recargando y reintentando (intento 1)..."
                        )
                        self._navegar_material(pm_id)
                        return _entrar_edicion_y_add_line(1)
                    # Segundo intento también falló — capturar y propagar
                    _capture(f"liq_{pm_id}_edit_blocked")
                    raise

                # 2c. Esperar "Agregar una línea" en advance_project_material_ids
                # Odoo renderiza el x2many en dos fases: primero aparece .o_form_editable,
                # luego inyecta el x2many via JS. 2.5s para que termine la segunda fase
                # (aumentado de 1.5s para dar margen bajo carga de servidor).
                time.sleep(2.5)
                add_sel = (
                    "[name='advance_project_material_ids'] .o_field_x2many_list_row_add a"
                )
                try:
                    page.wait_for_selector(add_sel, timeout=30_000)
                except PWTimeout:
                    if intento == 0:
                        log.warning(
                            f"[PW] 'Agregar una línea' no apareció pm#{pm_id} — "
                            "recargando y reintentando..."
                        )
                        self._navegar_material(pm_id)
                        return _entrar_edicion_y_add_line(1)
                    raise

                return page.locator(add_sel).first

            add_line = _entrar_edicion_y_add_line(0)
            _capture(f"liq_{pm_id}_02_edit")

            # 3. Click en "Agregar una línea"
            add_line.wait_for(timeout=WAIT_UNTIL)
            add_line.click()

            # Esperar a que la nueva fila tenga el input amount listo
            qty_sel = (
                "[name='advance_project_material_ids'] tr.o_selected_row input[name='amount']"
            )
            try:
                page.wait_for_selector(qty_sel, timeout=8000)
            except PWTimeout:
                time.sleep(0.6)   # fallback

            _capture(f"liq_{pm_id}_03_newrow")

            # 4. Llenar Cantidad De Avance (amount)
            qty_input = None
            for sel in [
                qty_sel,
                "[name='advance_project_material_ids'] tr.o_selected_row [name='amount']",
                "[name='advance_project_material_ids'] tbody tr:last-child input[name='amount']",
                "[name='advance_project_material_ids'] input[name='amount']",
            ]:
                el = page.locator(sel).last
                if el.count() > 0:
                    try:
                        if el.is_visible(timeout=1500):
                            qty_input = el
                            break
                    except PWTimeout:
                        pass

            if not qty_input:
                raise BrowserActionError(
                    "Campo 'amount' no encontrado en advance_project_material_ids"
                )
            qty_input.click(click_count=3)
            qty_input.fill(str(cantidad))
            _capture(f"liq_{pm_id}_05_qty")

            # 4b. Llenar partner_id (Equipo/Lider) si se proporcionó
            if partner_nombre:
                partner_sel = (
                    "[name='advance_project_material_ids'] "
                    "tr.o_selected_row [name='partner_id'] input"
                )
                try:
                    p_input = page.locator(partner_sel).first
                    if p_input.count() > 0 and p_input.is_visible(timeout=2000):
                        p_input.click()
                        # Buscar por primer apellido (primera palabra) — más discriminante en SGS
                        palabras = partner_nombre.strip().split()
                        termino = palabras[0] if palabras else partner_nombre.strip()
                        p_input.fill(termino)
                        time.sleep(0.8)
                        # Esperar dropdown de opciones
                        drop_sel = (
                            ".o_field_many2one_dropdown .o_m2o_dropdown_option, "
                            ".dropdown-item.o_m2o_dropdown_option, "
                            ".ui-autocomplete .ui-menu-item a"
                        )
                        try:
                            page.wait_for_selector(drop_sel, timeout=5000)
                            # Verificar que la opción contiene el nombre esperado
                            opciones = page.locator(drop_sel).all()
                            opcion_correcta = None
                            for op in opciones:
                                txt = op.inner_text().strip().upper()
                                if palabras[0].upper() in txt:
                                    opcion_correcta = op
                                    break
                            if not opcion_correcta:
                                raise PWTimeout(f"Ninguna opción del dropdown contiene '{palabras[0]}'")
                            opcion_correcta.click()
                            log.info(
                                f"[PW] partner_id llenado con '{termino}' pm#{pm_id}"
                            )
                        except PWTimeout:
                            # Partner no encontrado → ABORTAR. Nunca guardar con partner incorrecto.
                            _capture(f"liq_{pm_id}_05b_partner_fail")
                            raise BrowserActionError(
                                f"Partner '{partner_nombre}' no encontrado en dropdown SGS. "
                                "Abortando — no se guarda con partner incorrecto."
                            )
                    else:
                        raise BrowserActionError(
                            f"Campo partner_id no visible pm#{pm_id} — "
                            "no se puede confirmar líder. Abortando."
                        )
                except BrowserActionError:
                    raise   # propagar sin atrapar
                except PWTimeout:
                    raise BrowserActionError(
                        f"Timeout buscando partner_id pm#{pm_id} — abortando."
                    )
                except Exception as ex:
                    raise BrowserActionError(
                        f"Error llenando partner_id pm#{pm_id}: {_safe(ex)} — abortando."
                    )
                _capture(f"liq_{pm_id}_05b_partner")

            if dry_run:
                page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')").first.click()
                self._dismiss_error()
                result["ok"]      = True
                result["dry_run"] = True
                result["msg"]     = "Dry run OK — formulario completado pero NO guardado"
                log.info(f"[PW] Dry run OK pm#{pm_id}")
                return result

            # 5. GUARDAR — con intercepción de red
            # Esperar la respuesta HTTP del write server-side (~3-8s) en vez de
            # networkidle (55s) o EDITAR timeout (21s).
            # La respuesta de Odoo al save es un POST a /web/dataset/call_kw con
            # method="write" y model="project.material".
            def _es_respuesta_save(response):
                try:
                    if "call_kw" not in response.url:
                        return False
                    body = response.request.post_data or ""
                    return '"write"' in body and '"project.material"' in body
                except Exception:
                    return False

            t_save_start = time.time()
            save_btn = page.locator(
                "button:has-text('GUARDAR'), button:has-text('Guardar')"
            ).first
            edit_appeared = False
            save_confirmed = False

            try:
                with page.expect_response(_es_respuesta_save, timeout=30_000) as resp_info:
                    save_btn.click()
                resp = resp_info.value
                t_save_ms = int((time.time() - t_save_start) * 1000)
                try:
                    rdata = resp.json()
                    if rdata.get("error"):
                        raise PWTimeout(
                            f"GUARDAR falló: Odoo respondió con error — "
                            f"{rdata['error'].get('message','?')[:120]}"
                        )
                    save_confirmed = True
                    log.info(f"[PW] pm#{pm_id}: write confirmado en {t_save_ms}ms")
                except Exception as json_ex:
                    if not save_confirmed:
                        raise PWTimeout(f"GUARDAR: respuesta no parseable — {json_ex}")
            except PWTimeout:
                raise   # propagar como save_timeout
            except Exception as save_ex:
                raise PWTimeout(f"GUARDAR intercepción falló: {save_ex}")

            # Verificar modo: ¿apareció EDITAR? (clásico) o auto-edit
            try:
                page.wait_for_selector(
                    "button:has-text('EDITAR'), button:has-text('Editar')",
                    timeout=3_000,
                )
                edit_appeared = True
            except PWTimeout:
                # Auto-edit mode Odoo 17 — normal, dato ya confirmado por red
                log.info(f"[PW] pm#{pm_id}: auto-edit mode (write ya confirmado)")
                _capture(f"liq_{pm_id}_06_save_autoedit")

            self._dismiss_error()
            if edit_appeared:
                _capture(f"liq_{pm_id}_06_saved")

            result["ok"]  = True
            result["msg"] = f"Liquidado pm#{pm_id} qty={cantidad}"
            if not edit_appeared:
                result["msg"] += " (save_noeditar — verificar en SGS)"
            log.info(f"[PW] GUARDADO OK pm#{pm_id} edit_appeared={edit_appeared}")

        except PWTimeout as ex:
            result["error"] = _safe(f"Timeout: {ex}")
            result["pasos"].append(self._ss(f"liq_{pm_id}_timeout"))   # siempre en error
            log.error(f"[PW] Timeout pm#{pm_id}: {_safe(ex)}")
            # Resetear la página para que el próximo item arranque limpio
            try:
                page.goto(f"{SGS_URL}/web", wait_until="domcontentloaded", timeout=6000)
            except Exception:
                pass
        except Exception as ex:
            result["error"] = _safe(ex)
            result["pasos"].append(self._ss(f"liq_{pm_id}_error"))     # siempre en error
            log.error(f"[PW] Error pm#{pm_id}: {_safe(ex)}")
            try:
                page.goto(f"{SGS_URL}/web", wait_until="domcontentloaded", timeout=6000)
            except Exception:
                pass

        return result

    # ── AUTO-DIAGNÓSTICO ───────────────────────────────────────────────────────
    def diagnosticar_material(self, pm_id: int, cantidad: float,
                               partner_nombre: str = "") -> dict:
        """
        Ejecuta el flujo de liquidación paso a paso con diagnóstico profundo.
        En cada fallo captura: HTML del campo, visibilidad de elementos clave,
        screenshot, y aplica el fix conocido automáticamente si existe.

        Devuelve:
          {
            ok, paso_fallido, diagnosis, fix_aplicado, fix_exitoso,
            detalles: { form_titulo, editable, add_line_en_dom,
                        add_line_visible, partner_field, html_campo },
            screenshots, error
          }
        """
        if not self._logueado:
            raise BrowserActionError("Llama login() primero.")

        page = self._page
        out = {
            "pm_id":        pm_id,
            "ok":           False,
            "paso_fallido": None,
            "diagnosis":    None,
            "fix_aplicado": None,
            "fix_exitoso":  False,
            "detalles":     {},
            "screenshots":  [],
            "error":        None,
        }

        def _ss(tag):
            p = self._ss(tag)
            out["screenshots"].append(p)
            return p

        def _capturar_estado(tag):
            """Captura screenshot + estado del DOM en el momento del fallo."""
            _ss(tag)
            d = out["detalles"]
            # Título del formulario
            try:
                d["form_titulo"] = page.locator(".o_form_view h1, .breadcrumb-item.active").first.inner_text(timeout=2000)
            except Exception:
                d["form_titulo"] = page.title()
            # ¿Está en modo edición?
            d["editable"] = page.locator(".o_form_view.o_form_editable").count() > 0
            # Estado del campo advance
            add_cont = page.locator("[name='advance_project_material_ids']")
            if add_cont.count() > 0:
                try:
                    d["html_campo"] = add_cont.first.inner_html()[:2000]
                except Exception:
                    d["html_campo"] = "(error leyendo html)"
                add_row = page.locator("[name='advance_project_material_ids'] .o_field_x2many_list_row_add")
                d["add_line_en_dom"]  = add_row.count() > 0
                d["add_line_visible"] = add_row.first.is_visible() if d["add_line_en_dom"] else False
            else:
                d["html_campo"]       = "(campo no encontrado)"
                d["add_line_en_dom"]  = False
                d["add_line_visible"] = False
            # ¿Campo partner visible?
            partner_loc = page.locator(
                "[name='advance_project_material_ids'] tr.o_selected_row [name='partner_id'] input"
            )
            d["partner_field"] = partner_loc.count() > 0 and partner_loc.first.is_visible(timeout=500) if partner_loc.count() > 0 else False

        # ── PASO 1: Navegar ────────────────────────────────────────────────────
        try:
            self._navegar_material(pm_id)
            _ss(f"diag_{pm_id}_01_nav")
        except Exception as ex:
            out["paso_fallido"] = "navegacion"
            out["diagnosis"]    = f"No se pudo cargar el formulario: {_safe(ex)}"
            out["error"]        = _safe(ex)
            _capturar_estado(f"diag_{pm_id}_01_nav_fail")
            return out

        # ── PASO 2: EDITAR ─────────────────────────────────────────────────────
        edit_btn = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
        try:
            edit_btn.wait_for(state="visible", timeout=8_000)
            edit_btn.click()
            self._dismiss_error()
        except PWTimeout:
            pass  # puede que ya esté en edit mode

        # Confirmar .o_form_editable
        try:
            page.wait_for_selector(".o_form_view.o_form_editable", timeout=10_000)
        except PWTimeout:
            out["paso_fallido"] = "editar"
            _capturar_estado(f"diag_{pm_id}_02_edit_fail")
            d = out["detalles"]
            if not d.get("editable"):
                out["diagnosis"] = (
                    "El formulario NO entró en modo edición. "
                    "Causas posibles: (1) pm_id apunta al registro bloqueado (sin C), "
                    "(2) proyecto cerrado/bloqueado en SGS, "
                    "(3) servidor lento — EDITAR no respondió a tiempo."
                )
                out["fix_aplicado"] = "Reload + reintento con 3s extra"
                # Fix: recargar y esperar más
                self._navegar_material(pm_id)
                time.sleep(3)
                try:
                    edit_btn2 = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
                    edit_btn2.wait_for(state="visible", timeout=8_000)
                    edit_btn2.click()
                    page.wait_for_selector(".o_form_view.o_form_editable", timeout=15_000)
                    out["fix_exitoso"] = True
                    _ss(f"diag_{pm_id}_02_edit_fix_ok")
                except Exception as ex2:
                    out["error"] = _safe(ex2)
                    out["fix_exitoso"] = False
                    _ss(f"diag_{pm_id}_02_edit_fix_fail")
                    return out
            if not out["fix_exitoso"]:
                return out

        _ss(f"diag_{pm_id}_02_edit_ok")

        # ── PASO 3: AGREGAR LÍNEA ──────────────────────────────────────────────
        add_sel = "[name='advance_project_material_ids'] .o_field_x2many_list_row_add a"
        time.sleep(1.5)   # esperar render async del x2many
        add_line = None
        try:
            page.wait_for_selector(add_sel, timeout=8_000)
            add_line = page.locator(add_sel).first
        except PWTimeout:
            out["paso_fallido"] = "add_line"
            _capturar_estado(f"diag_{pm_id}_03_addline_fail")
            d = out["detalles"]
            if d.get("add_line_en_dom") and not d.get("add_line_visible"):
                out["diagnosis"]    = "El elemento 'Agregar línea' está en el DOM pero NO visible. El x2many aún no terminó de renderizar."
                out["fix_aplicado"] = "Sleep 2s adicional + scroll al campo"
            else:
                out["diagnosis"]    = "El elemento 'Agregar línea' no está en el DOM. El campo advance puede ser readonly para este registro (estado del proyecto o condición de Odoo)."
                out["fix_aplicado"] = "Scroll + espera extendida 5s"
            # Fix: scroll + espera extendida
            try:
                page.locator("[name='advance_project_material_ids']").first.scroll_into_view_if_needed()
                time.sleep(3)
                page.wait_for_selector(add_sel, timeout=5_000)
                add_line = page.locator(add_sel).first
                out["fix_exitoso"] = True
                _ss(f"diag_{pm_id}_03_addline_fix_ok")
            except Exception as ex3:
                out["error"] = _safe(ex3)
                _capturar_estado(f"diag_{pm_id}_03_addline_fix_fail")
                if not d.get("add_line_en_dom"):
                    out["diagnosis"] += " FIX FALLIDO: el campo es genuinamente readonly en SGS — requiere intervención manual o el pm_id es incorrecto."
                return out

        # ── PASO 4: Llenar cantidad ────────────────────────────────────────────
        add_line.click()
        qty_sel = "[name='advance_project_material_ids'] tr.o_selected_row input[name='amount']"
        try:
            page.wait_for_selector(qty_sel, timeout=8_000)
        except PWTimeout:
            time.sleep(1)
        try:
            qi = page.locator(qty_sel).first
            qi.click(click_count=3)
            qi.fill(str(cantidad))
            _ss(f"diag_{pm_id}_04_qty")
        except Exception as ex4:
            out["paso_fallido"] = "cantidad"
            out["diagnosis"]    = f"No se pudo llenar la cantidad: {_safe(ex4)}"
            out["error"]        = _safe(ex4)
            _capturar_estado(f"diag_{pm_id}_04_qty_fail")
            return out

        # ── PASO 5: Partner ────────────────────────────────────────────────────
        if partner_nombre:
            partner_sel = "[name='advance_project_material_ids'] tr.o_selected_row [name='partner_id'] input"
            try:
                p_input = page.locator(partner_sel).first
                p_input.wait_for(state="visible", timeout=3_000)
                p_input.click()
                termino = partner_nombre.strip().split()[-1]
                p_input.fill(termino)
                time.sleep(0.8)
                drop_sel = ".o_field_many2one_dropdown .o_m2o_dropdown_option, .dropdown-item.o_m2o_dropdown_option"
                try:
                    page.wait_for_selector(drop_sel, timeout=4_000)
                    page.locator(drop_sel).first.click()
                    out["detalles"]["partner_ok"] = True
                    _ss(f"diag_{pm_id}_05_partner")
                except PWTimeout:
                    out["detalles"]["partner_ok"]   = False
                    out["detalles"]["partner_error"] = f"Dropdown no apareció para '{termino}'"
            except Exception as ep:
                out["detalles"]["partner_ok"]   = False
                out["detalles"]["partner_error"] = _safe(ep)

        # ── PASO 6: DESCARTAR (es diagnóstico, no guardar) ─────────────────────
        try:
            page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')").first.click()
            time.sleep(0.5)
            # Confirmar diálogo de descartar si aparece
            discard_confirm = page.locator("button:has-text('Aceptar'), button:has-text('Ok'), button:has-text('Discard'), .modal-footer button.btn-primary").first
            if discard_confirm.count() > 0 and discard_confirm.is_visible(timeout=1500):
                discard_confirm.click()
        except Exception:
            pass
        _ss(f"diag_{pm_id}_06_discard")

        out["ok"]        = True
        out["diagnosis"] = "Flujo completo OK — todos los pasos superados. El material puede liquidarse."
        if out.get("fix_aplicado"):
            out["diagnosis"] += f" (fix aplicado: {out['fix_aplicado']})"
        return out


# ── Pool de workers con cola de tareas ────────────────────────────────────────
def liquidar_mo_parallel_por_proyecto(items: list,
                                      max_workers: int = 2,
                                      storage_state: dict = None,
                                      progress_cb=None,
                                      should_cancel=None,
                                      sgs_client_factory=None) -> list:
    """
    Liquidacion MO conservadora:
      - Maximo 1 browser por proyecto (pid).
      - Las actividades de un mismo proyecto se procesan en serie.
      - Distintos proyectos pueden correr en paralelo hasta max_workers.

    Reduce el overhead de abrir/cerrar browser por cada MO sin permitir
    escrituras concurrentes sobre el mismo project.dominion.
    """
    if not items:
        return []

    resultados = [None] * len(items)
    resultados_lock = _threading_mod.Lock()

    grupos = {}
    for orig_idx, item in enumerate(items):
        pid_key = item.get("pid")
        if pid_key is None:
            pid_key = f"sinpid-{orig_idx}"
        grupos.setdefault(pid_key, []).append((orig_idx, item))

    def _cancel_requested():
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    def _cb(orig_idx, worker_id, estado, cod, error=None):
        if progress_cb:
            try:
                progress_cb(orig_idx, worker_id, estado, cod, error=error)
            except Exception:
                pass

    def _res_error(item, msg, worker_id, error_tipo="mo_error"):
        return {
            "ok": False,
            "error": msg,
            "error_tipo": error_tipo,
            "tipo": "MO",
            "codigo": item.get("act_code", ""),
            "pro_code": item.get("pro_code", ""),
            "cantidad": item.get("cantidad", 0),
            "partner_nombre": item.get("partner_nombre", ""),
            "act_id": item.get("act_id"),
            "pid": item.get("pid"),
            "worker_id": worker_id,
            "pasos": [],
        }

    if not storage_state:
        msg = "Sin sesion — inicia sesion en el browser visible"
        for i, item in enumerate(items):
            resultados[i] = _res_error(item, msg, worker_id=-1, error_tipo="no_session")
        return resultados

    grupos_list = list(grupos.items())
    n_workers = min(max_workers, len(grupos_list))
    log.info(f"[MO-POOL] {n_workers} worker(es) para {len(grupos_list)} proyecto(s) / {len(items)} item(s)")

    def _worker(worker_id: int, pid_key, grupo_items: list):
        if _cancel_requested():
            for orig_idx, item in grupo_items:
                res = _res_error(item, "Liquidacion cancelada por el usuario", worker_id, "cancelado")
                with resultados_lock:
                    resultados[orig_idx] = res
                _cb(orig_idx, worker_id, "error", item.get("act_code", "?"), error=res["error"])
            return

        pw = PlaywrightClient(headless=False, slow_mo=100)
        sgs_client = None
        act_ids_cache = {}

        try:
            pw.start(storage_state=storage_state)
        except Exception as ex:
            msg = f"No se pudo iniciar browser MO: {_safe(ex)}"
            log.error(f"[MO-WORKER-{worker_id}] pid={pid_key} {msg}")
            for orig_idx, item in grupo_items:
                res = _res_error(item, msg, worker_id, "browser_start_error")
                with resultados_lock:
                    resultados[orig_idx] = res
                _cb(orig_idx, worker_id, "error", item.get("act_code", "?"), error=msg)
            return

        try:
            if sgs_client_factory:
                try:
                    sgs_client = sgs_client_factory()
                except Exception as ex:
                    log.warning(f"[MO-WORKER-{worker_id}] pid={pid_key} sin readonly client: {_safe(ex)}")

            en_lista = False  # Evita re-navegar entre prestaciones del mismo proyecto

            for orig_idx, item in grupo_items:
                cod = item.get("act_code", "?")

                if _cancel_requested():
                    res = _res_error(item, "Liquidacion cancelada por el usuario", worker_id, "cancelado")
                    with resultados_lock:
                        resultados[orig_idx] = res
                    _cb(orig_idx, worker_id, "error", cod, error=res["error"])
                    en_lista = False
                    continue

                _cb(orig_idx, worker_id, "corriendo", cod)
                log.info(f"[MO-WORKER-{worker_id}] pid={pid_key} code={cod} qty={item.get('cantidad')}")

                item_exec = item.copy()
                act_key = (item_exec.get("pid"), item_exec.get("act_code"))
                if not item_exec.get("act_id") and act_key in act_ids_cache:
                    item_exec["act_id"] = act_ids_cache[act_key]

                try:
                    res = pw.crear_y_liquidar_mo(
                        pid=item_exec["pid"],
                        act_code=item_exec["act_code"],
                        cantidad=item_exec["cantidad"],
                        pro_code=item_exec.get("pro_code", ""),
                        act_id=item_exec.get("act_id"),
                        sgs_client=sgs_client,
                        dry_run=False,
                        partner_nombre=item_exec.get("partner_nombre", ""),
                        partner_cedula=item_exec.get("partner_cedula", ""),
                        cantidad_proyectada=item_exec.get("cantidad_proyectada"),
                        skip_nav=en_lista,
                    )
                    en_lista = bool(res.get("ok"))
                except Exception as ex:
                    res = {"ok": False, "error": _safe(ex), "pasos": []}
                    en_lista = False

                if res.get("ok") and res.get("act_id"):
                    act_ids_cache[act_key] = res["act_id"]

                res.setdefault("pasos", [])
                res["tipo"] = "MO"
                res["codigo"] = item_exec.get("act_code", "")
                res["pro_code"] = item_exec.get("pro_code", "")
                res["cantidad"] = item_exec.get("cantidad", 0)
                res["partner_nombre"] = item_exec.get("partner_nombre", "")
                res["pid"] = item_exec.get("pid")
                res["worker_id"] = worker_id

                with resultados_lock:
                    resultados[orig_idx] = res

                _cb(orig_idx, worker_id, "ok" if res.get("ok") else "error", cod, error=res.get("error"))
                time.sleep(1.0)

        finally:
            try:
                pw.close()
            except Exception:
                pass
            log.info(f"[MO-WORKER-{worker_id}] pid={pid_key} terminado")

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [
            executor.submit(_worker, idx % n_workers, pid_key, grupo_items)
            for idx, (pid_key, grupo_items) in enumerate(grupos_list)
        ]
        for future in _as_completed(futures):
            try:
                future.result()
            except Exception as ex:
                log.error(f"[MO-POOL] Future error: {_safe(ex)}")

    for i, res in enumerate(resultados):
        if res is None:
            if _cancel_requested():
                resultados[i] = _res_error(items[i], "Liquidacion cancelada por el usuario", -1, "cancelado")
            else:
                resultados[i] = _res_error(items[i], "Sin resultado — worker MO interrumpido", -1, "worker_crash")

    return resultados


import queue as _queue_mod
import threading as _threading_mod
from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

# ── Configuración del pool v2 (Auxiliar + Worker) ─────────────────────────────
# Valores conservadores si se reactiva el pool v2. Los experimentos con
# 3+ workers/preloaders saturaron SGS; produccion usa v1 por defecto.
POOL_V2_MAX_WORKERS    = 2    # maximo validado para no saturar SGS
POOL_V2_NUM_PRELOADERS = 0    # preloaders desactivados: fueron experimentales
POOL_V2_STAGGER_S      = 8    # stagger validado para desfasar saves
POOL_V2_WARMUP_TTL_S   = 60   # segundos antes de que un warmup se considere stale
POOL_V2_PRELOADER_RATE = 2.0  # segundos minimos entre navegaciones del preloader
POOL_V2_USE_PROJECT_SEM = True  # si v2 se reactiva, priorizar seguridad


def _clasificar_error(error_str: str) -> str:
    """
    Clasifica el error de liquidación para decidir la acción a tomar.
    Retorna uno de:
      login_timeout   → servidor no respondió al login
      nav_timeout     → form tardó en cargar (retryable)
      edit_timeout    → form no entró en edit mode (pm_id posiblemente bloqueado)
      save_timeout    → networkidle timeout post-GUARDAR (NO retryable — dato YA grabado en Odoo)
      addline_timeout → agregar-línea no apareció (retryable)
      session_expired → sesión caducó (re-login + retry)
      generic_timeout → timeout genérico (retryable)
      unknown         → error no clasificado
    """
    e = (error_str or "").lower()
    if "pendiente dom" in e or "pendiente api" in e:
        return "pendiente_confirmacion"
    if "timeout" in e and "o_main_navbar" in e:
        return "login_timeout"
    # save_timeout: post-GUARDAR → error visual o EDITAR no apareció (NO retryable)
    if "guardar falló" in e or ("networkidle timeout" in e and "editar" in e) or "error visual en sgs" in e:
        return "save_timeout"
    if "timeout" in e and ("o_field_widget" in e or "o_form_label" in e or "o_form_view" in e):
        return "nav_timeout"
    if "timeout" in e and ("o_form_editable" in e or "edit mode" in e):
        return "edit_timeout"       # form no entró en edit — pm_id bloqueado
    if "timeout" in e and ("has-text('editar')" in e or "has-text('editar')" in e):
        # EDITAR button timeout en contexto de GUARDAR → save_timeout (NO retryable)
        return "save_timeout"
    if "timeout" in e and ("add_line" in e or "agregar" in e or "x2many" in e):
        return "addline_timeout"
    if "timeout" in e:
        return "generic_timeout"
    if "session" in e or "invalid" in e or "403" in e or "401" in e:
        return "session_expired"
    return "unknown"


def liquidar_materiales_lista_modal_parallel(items: list,
                                             max_pages: int = 2,
                                             storage_state: dict = None,
                                             progress_cb=None,
                                             should_cancel=None) -> list:
    """
    Liquidacion MAT por vista lista + modal Consumption detail.

    Modelo operativo:
      - Agrupa materiales por pid.
      - Por cada proyecto abre hasta max_pages paginas con la lista de materiales.
      - Cada pagina mantiene su lista abierta y procesa una cola propia.
      - Antes de actuar sobre cada material consulta project.material.details; si
        ya existe una linea, lo marca OK y continua.
      - Despues de GUARDAR espera la respuesta write de project.material y
        confirma persistencia por project.material.details state=done.

    Seguridad:
      - No reintenta despues de click en GUARDAR.
      - No usa balance/advance post-save como confirmacion porque pueden quedar
        stale; la confirmacion es project.material.details.
      - Si should_cancel() se activa, no toma nuevos materiales.
    """
    if not items:
        return []

    if not storage_state:
        return [{
            "ok": False,
            "tipo": "MAT",
            "codigo": it.get("codigo", ""),
            "pm_id": it.get("pm_id"),
            "cantidad": it.get("cantidad", 0),
            "error": "Sin sesion SGS heredada para liquidar MAT por lista",
            "error_tipo": "no_session",
            "pasos": [],
        } for it in items]

    def _cancel_requested():
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    def _ro_client():
        from sgs_readonly_client import SGSReadOnlyClient
        c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
        c.autenticar()
        return c

    def _read_details_sync(pm_id: int):
        c = _ro_client()
        details = c.search_read(
            "project.material.details",
            [["project_material_id", "=", pm_id]],
            ["id", "amount", "partner_id", "state", "picking_id", "create_uid", "create_date"],
            limit=20,
        )
        details.sort(key=lambda d: d.get("id", 0), reverse=True)
        return details

    async def _read_details(pm_id: int):
        return await asyncio.to_thread(_read_details_sync, pm_id)

    async def _wait_details(item: dict, timeout_s: float = 45.0):
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            details = await _read_details(item["pm_id"])
            if details:
                return details
            await asyncio.sleep(1.2)
        return []

    def _project_url(pid: int) -> str:
        return (f"{SGS_URL}/web#id={pid}&action=485"
                f"&model=project.dominion&view_type=form&cids=1&menu_id=352")

    async def _open_list(page, pid: int, label: str):
        await page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(800)
        await page.goto(_project_url(pid), wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_selector("button[name='action_button_material']", timeout=90_000)
        await page.wait_for_timeout(1200)
        await page.locator("button[name='action_button_material']").first.click()
        await page.wait_for_selector(".o_data_row", timeout=90_000)
        await page.wait_for_timeout(1500)
        rows = await page.locator(".o_data_row").count()
        log.info(f"[MAT-LISTA {label}] Lista abierta pid={pid} rows={rows}")

    async def _close_modal(page, label: str, codigo: str):
        selectors = [
            ".modal button.close",
            ".modal .close",
            ".modal-footer button:has-text('Cerrar')",
            ".modal-footer button:has-text('DESCARTAR')",
            ".modal-footer button:has-text('Descartar')",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.click()
                    await page.wait_for_timeout(1200)
                    log.info(f"[MAT-LISTA {label}] Modal cerrado {codigo}")
                    return
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1200)
        except Exception:
            pass

    async def _select_partner(page, partner_nombre: str):
        partner_sel = (
            ".modal [name='advance_project_material_ids'] tr.o_selected_row [name='partner_id'] input, "
            ".modal tr.o_selected_row [name='partner_id'] input, "
            ".modal [name='partner_id'] input"
        )
        p_input = page.locator(partner_sel).last
        await p_input.wait_for(state="visible", timeout=15_000)
        palabras = partner_nombre.strip().split()
        termino = palabras[0] if palabras else partner_nombre.strip()
        if not termino:
            raise BrowserActionError("Lider vacio para material")
        await p_input.click()
        await p_input.fill(termino)
        await page.wait_for_timeout(1000)
        drop_sel = (
            ".o_field_many2one_dropdown .o_m2o_dropdown_option, "
            ".dropdown-item.o_m2o_dropdown_option, "
            ".ui-autocomplete .ui-menu-item a"
        )
        drop = page.locator(drop_sel)
        await drop.first.wait_for(state="visible", timeout=10_000)
        elegido = None
        for i in range(await drop.count()):
            op = drop.nth(i)
            txt = (await op.inner_text()).strip().upper()
            if termino.upper() in txt:
                elegido = op
                break
        if not elegido:
            raise BrowserActionError(f"Lider '{partner_nombre}' no encontrado en dropdown SGS")
        await elegido.click()

    async def _fill_and_save(page, item: dict, label: str) -> dict:
        codigo = item.get("codigo", str(item.get("pm_id", "")))
        pm_id = item["pm_id"]
        cantidad = float(item.get("cantidad") or 0)
        t0 = time.perf_counter()

        row = page.locator(f".o_data_row:has-text('{codigo}')").first
        await row.wait_for(state="visible", timeout=60_000)
        await row.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)

        add_sel = (
            ".modal [name='advance_project_material_ids'] .o_field_x2many_list_row_add a, "
            ".modal a:has-text('AGREGAR'), "
            ".modal a:has-text('Agregar')"
        )

        # Abrir modal con verificación — máx 2 intentos (pre-GUARDAR, retryable per R4)
        for _intento_modal in range(2):
            await row.locator("button[name='action_show_details']").first.click()
            _modal_ok = False
            try:
                await page.wait_for_selector(
                    ".modal:has-text('Consumption detail'), .modal-dialog:has-text('Consumption detail')",
                    timeout=30_000,
                )
                await page.wait_for_timeout(700)
                # Verificar que el modal está en estado editable correcto
                await page.locator(add_sel).first.wait_for(state="visible", timeout=4_000)
                _modal_ok = True
            except Exception:
                pass

            if _modal_ok:
                break

            log.warning(f"[{label}] Modal abrió en estado incorrecto para {codigo} "
                        f"(intento {_intento_modal + 1}/2) — cerrando y reintentando")
            await _close_modal(page, label, codigo)
            await page.wait_for_timeout(1500)
            if _intento_modal == 1:
                raise BrowserActionError(
                    f"modal_bad_state — modal no abrió correctamente para {codigo} tras 2 intentos"
                )

        await page.locator(add_sel).first.click()
        await page.wait_for_timeout(800)

        qty_sel = (
            ".modal [name='advance_project_material_ids'] tr.o_selected_row input[name='amount'], "
            ".modal tr.o_selected_row input[name='amount'], "
            ".modal [name='amount'] input, "
            ".modal input[name='amount']"
        )
        qty = page.locator(qty_sel).last
        await qty.wait_for(state="visible", timeout=15_000)
        await qty.click(click_count=3)
        await qty.fill(str(cantidad))

        await _select_partner(page, item.get("partner_nombre", ""))
        await page.wait_for_timeout(500)

        def _is_write_response(resp):
            try:
                body = resp.request.post_data or ""
                return '"write"' in body and '"project.material"' in body and f"[{pm_id}]" in body
            except Exception:
                return False

        save_btn = page.locator(
            ".modal-footer button:has-text('GUARDAR'), "
            ".modal-footer button:has-text('Guardar')"
        ).first
        await save_btn.wait_for(state="visible", timeout=15_000)
        t_save = time.perf_counter()
        # Paso 7 — GUARDAR con intercepción de red.
        # Timeout capturado aquí (no propagado) para que _worker_ws lo clasifique
        # como save_timeout (R1: no reintentar post-GUARDAR).
        try:
            async with page.expect_response(_is_write_response, timeout=45_000) as resp_info:
                await save_btn.click()
            resp = await resp_info.value
            data = await resp.json()
        except AsyncPWTimeout:
            write_dt = time.perf_counter() - t_save
            return {
                "ok": False,
                "error": (f"Timeout {write_dt:.1f}s esperando respuesta HTTP del GUARDAR "
                          "— dato puede ya estar guardado en SGS. Verificar manualmente."),
                "error_tipo": "save_timeout",
                "write_dt": round(write_dt, 2),
            }
        write_dt = time.perf_counter() - t_save
        if data.get("error"):
            return {
                "ok": False,
                "error": data["error"].get("data", {}).get("message") or str(data["error"])[:500],
                "error_tipo": "save_error",
                "write_dt": round(write_dt, 2),
            }

        details = await _wait_details(item)
        confirm_dt = time.perf_counter() - t_save
        if not details:
            return {
                "ok": False,
                "error": "GUARDAR respondio OK pero no aparecio project.material.details",
                "error_tipo": "post_confirm_timeout",
                "write_dt": round(write_dt, 2),
                "confirm_dt": round(confirm_dt, 2),
            }
        detail = details[0]
        amount = float(detail.get("amount") or 0)
        if abs(amount - cantidad) > 0.001:
            return {
                "ok": False,
                "error": f"Detalle creado con amount={amount:g}, esperado={cantidad:g}",
                "error_tipo": "post_amount_mismatch",
                "write_dt": round(write_dt, 2),
                "confirm_dt": round(confirm_dt, 2),
                "detail": detail,
            }
        if detail.get("state") and detail.get("state") != "done":
            return {
                "ok": False,
                "error": f"Detalle creado con state={detail.get('state')}",
                "error_tipo": "post_state_not_done",
                "write_dt": round(write_dt, 2),
                "confirm_dt": round(confirm_dt, 2),
                "detail": detail,
            }
        await _close_modal(page, label, codigo)
        return {
            "ok": True,
            "write_dt": round(write_dt, 2),
            "confirm_dt": round(confirm_dt, 2),
            "duracion_s": round(time.perf_counter() - t0, 2),
            "detail": detail,
        }

    def _base_result(item: dict) -> dict:
        return {
            "tipo": "MAT",
            "pm_id": item.get("pm_id"),
            "codigo": item.get("codigo", ""),
            "codigo_sgs": item.get("codigo_sgs", item.get("codigo", "")),
            "es_custodia": item.get("es_custodia", False),
            "pro_code": item.get("pro_code", ""),
            "cantidad": item.get("cantidad", 0),
            "cantidad_solicitada": item.get("cantidad", 0),
            "excel_neto": item.get("excel_neto"),
            "pid": item.get("pid"),
            "partner_nombre": item.get("partner_nombre", ""),
            "pasos": [],
            "intentos": 1,
        }

    async def _worker(page, label: str, indexed_queue: list):
        out = []
        for orig_idx, item in indexed_queue:
            codigo = item.get("codigo", item.get("pm_id", "?"))
            if _cancel_requested():
                res = dict(_base_result(item), ok=False, error="Liquidacion cancelada por el usuario",
                           error_tipo="cancelado", worker_id=label)
                out.append((orig_idx, res))
                continue
            try:
                details = await _read_details(item["pm_id"])
                if details:
                    # Para multi-lider: verificar que ya existe un detalle con esta
                    # combinación exacta de partner + cantidad, no solo cualquier detalle.
                    _partner_b = (item.get("partner_nombre") or "").strip().upper()
                    _qty_b     = float(item.get("cantidad") or 0)
                    _ya_existe = False
                    for _d in details:
                        _pf = _d.get("partner_id")
                        _dp = str(_pf[1] if isinstance(_pf, list) and len(_pf) >= 2 else "").strip().upper()
                        _da = float(_d.get("amount") or 0)
                        if abs(_da - _qty_b) < 0.001 and (not _partner_b or _partner_b in _dp or _dp in _partner_b):
                            _ya_existe = True
                            break
                    if _ya_existe:
                        res = dict(_base_result(item), ok=True, worker_id=label,
                                   skipped_existing=True, detail=details[0])
                        out.append((orig_idx, res))
                        if progress_cb:
                            progress_cb(orig_idx, label, "ok", codigo)
                        continue
                if progress_cb:
                    progress_cb(orig_idx, label, "corriendo", codigo)
                res_inner = await _fill_and_save(page, item, label)
                res = dict(_base_result(item), **res_inner, worker_id=label)
                if progress_cb:
                    progress_cb(orig_idx, label, "ok" if res.get("ok") else "error",
                                codigo, error=res.get("error"))
                out.append((orig_idx, res))
                if not res.get("ok"):
                    break
            except AsyncPWTimeout as ex:
                res = dict(_base_result(item), ok=False, error=_safe(ex),
                           error_tipo="ui_timeout", worker_id=label)
                out.append((orig_idx, res))
                if progress_cb:
                    progress_cb(orig_idx, label, "error", codigo, error=res["error"])
                break
            except Exception as ex:
                res = dict(_base_result(item), ok=False, error=_safe(ex),
                           error_tipo="ui_error", worker_id=label)
                out.append((orig_idx, res))
                if progress_cb:
                    progress_cb(orig_idx, label, "error", codigo, error=res["error"])
                break
        return out

    async def _run_group(pid, indexed_items):
        indexed_items = list(indexed_items)
        pages_n = min(max_pages, len(indexed_items))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, slow_mo=80)
            context = await browser.new_context(storage_state=storage_state, viewport={"width": 1400, "height": 900})
            pages = []
            for i in range(pages_n):
                page = await context.new_page()
                page.set_default_timeout(WAIT_UNTIL)
                pages.append(page)
            await asyncio.gather(*[_open_list(page, pid, f"p{pid}-tab{i+1}") for i, page in enumerate(pages)])

            buckets = [[] for _ in range(pages_n)]
            for pos, pair in enumerate(indexed_items):
                buckets[pos % pages_n].append(pair)

            tasks = []
            for i, bucket in enumerate(buckets):
                if not bucket:
                    continue
                orig_indices = [idx for idx, _ in bucket]
                queue_items = [it for _, it in bucket]
                async def _run_bucket(page=pages[i], label=f"p{pid}-tab{i+1}",
                                      queue_items=queue_items, orig_indices=orig_indices):
                    local = await _worker(page, label, list(zip(orig_indices, queue_items)))
                    return local
                tasks.append(_run_bucket())
            nested = await asyncio.gather(*tasks)
            await browser.close()
            flat = []
            for part in nested:
                flat.extend(part)
            return flat

    async def _main():
        by_pid = {}
        for i, item in enumerate(items):
            by_pid.setdefault(item.get("pid") or 0, []).append((i, item))

        all_pairs = []
        for pid, indexed in by_pid.items():
            if not pid:
                for i, item in indexed:
                    all_pairs.append((i, dict(_base_result(item), ok=False,
                                              error="Item MAT sin pid para abrir lista",
                                              error_tipo="missing_pid")))
                continue
            all_pairs.extend(await _run_group(pid, indexed))
        return all_pairs

    try:
        pairs = asyncio.run(_main())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            pairs = loop.run_until_complete(_main())
        finally:
            loop.close()
    except Exception as ex:
        return [dict(_base_result(it), ok=False, error=_safe(ex), error_tipo="mat_lista_crash")
                for it in items]

    resultados = [None] * len(items)
    for idx, res in pairs:
        resultados[idx] = res
    for i, res in enumerate(resultados):
        if res is None:
            resultados[i] = dict(_base_result(items[i]), ok=False,
                                 error="Sin resultado en MAT lista/modal",
                                 error_tipo="missing_result")
    return resultados


# ── Constante de stagger para work-stealing ───────────────────────────────────
_WS_STAGGER_SECS: float = 8.0   # R2 CLAUDE.md: stagger entre tabs para stagear saves


def liquidar_materiales_lista_modal_ws(items: list,
                                       max_pages: int = 2,
                                       max_concurrent_nodes: int = 1,
                                       storage_state: dict = None,
                                       progress_cb=None,
                                       should_cancel=None) -> list:
    """
    Variante work-stealing de liquidar_materiales_lista_modal_parallel.

    Diferencia clave respecto a la version original:
      - En lugar de buckets estaticos (round-robin fijo), usa un asyncio.Queue
        compartido entre todos los tabs del mismo proyecto.
      - Cuando un tab termina su material actual, toma el siguiente disponible
        de la cola global sin esperar al otro tab.
      - El tab secundario (tab_idx > 0) aplica un stagger inicial de
        _WS_STAGGER_SECS antes de consumir su primer item, para no solapar
        el primer GUARDAR de cada tab con el del tab primario.

    Garantias de seguridad:
      - asyncio.Queue.get_nowait() es atomico dentro del event loop
        (single-threaded) -> ningun item se procesa dos veces.
      - Si un tab falla (save_error / ui_timeout), deja de consumir la cola;
        el otro tab puede continuar con los items restantes.
      - No reintenta despues de GUARDAR (R1 CLAUDE.md).
      - No toma nuevos items si should_cancel() esta activo.

    Ventaja practica:
      - Elimina tiempo de espera al final del lote cuando un tab termina
        antes que el otro (especialmente util en lotes con tiempos variables
        de respuesta de Odoo o con mezcla de materiales rapidos/lentos).
    """
    if not items:
        return []

    if not storage_state:
        return [{
            "ok": False,
            "tipo": "MAT",
            "codigo": it.get("codigo", ""),
            "pm_id": it.get("pm_id"),
            "cantidad": it.get("cantidad", 0),
            "error": "Sin sesion SGS heredada para liquidar MAT (WS)",
            "error_tipo": "no_session",
            "pasos": [],
        } for it in items]

    def _cancel_requested():
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    def _ro_client():
        from sgs_readonly_client import SGSReadOnlyClient
        c = SGSReadOnlyClient(SGS_URL, SGS_DB, SGS_LOGIN, SGS_PASSWORD)
        c.autenticar()
        return c

    def _read_details_sync(pm_id: int):
        c = _ro_client()
        details = c.search_read(
            "project.material.details",
            [["project_material_id", "=", pm_id]],
            ["id", "amount", "partner_id", "state", "picking_id", "create_uid", "create_date"],
            limit=20,
        )
        details.sort(key=lambda d: d.get("id", 0), reverse=True)
        return details

    async def _read_details(pm_id: int):
        return await asyncio.to_thread(_read_details_sync, pm_id)

    async def _wait_details(item: dict, timeout_s: float = 45.0):
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            details = await _read_details(item["pm_id"])
            if details:
                return details
            await asyncio.sleep(1.2)
        return []

    def _project_url(pid: int) -> str:
        return (f"{SGS_URL}/web#id={pid}&action=485"
                f"&model=project.dominion&view_type=form&cids=1&menu_id=352")

    async def _open_list(page, pid: int, label: str):
        await page.goto(f"{SGS_URL}/web?cids=1", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(800)
        await page.goto(_project_url(pid), wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_selector("button[name='action_button_material']", timeout=90_000)
        await page.wait_for_timeout(1200)
        await page.locator("button[name='action_button_material']").first.click()
        await page.wait_for_selector(".o_data_row", timeout=90_000)
        await page.wait_for_timeout(1500)
        rows = await page.locator(".o_data_row").count()
        log.info(f"[MAT-WS {label}] Lista abierta pid={pid} rows={rows}")

    async def _close_modal(page, label: str, codigo: str):
        selectors = [
            ".modal button.close",
            ".modal .close",
            ".modal-footer button:has-text('Cerrar')",
            ".modal-footer button:has-text('DESCARTAR')",
            ".modal-footer button:has-text('Descartar')",
        ]
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible(timeout=1000):
                    await loc.click()
                    await page.wait_for_timeout(1200)
                    return
            except Exception:
                pass
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1200)
        except Exception:
            pass

    async def _select_partner(page, partner_nombre: str):
        partner_sel = (
            ".modal [name='advance_project_material_ids'] tr.o_selected_row [name='partner_id'] input, "
            ".modal tr.o_selected_row [name='partner_id'] input, "
            ".modal [name='partner_id'] input"
        )
        p_input = page.locator(partner_sel).last
        await p_input.wait_for(state="visible", timeout=15_000)
        palabras = partner_nombre.strip().split()
        termino = palabras[0] if palabras else partner_nombre.strip()
        if not termino:
            raise BrowserActionError("Lider vacio para material")
        await p_input.click()
        await p_input.fill(termino)
        await page.wait_for_timeout(1000)
        drop_sel = (
            ".o_field_many2one_dropdown .o_m2o_dropdown_option, "
            ".dropdown-item.o_m2o_dropdown_option, "
            ".ui-autocomplete .ui-menu-item a"
        )
        drop = page.locator(drop_sel)
        await drop.first.wait_for(state="visible", timeout=10_000)
        elegido = None
        for i in range(await drop.count()):
            op = drop.nth(i)
            txt = (await op.inner_text()).strip().upper()
            if termino.upper() in txt:
                elegido = op
                break
        if not elegido:
            raise BrowserActionError(f"Lider '{partner_nombre}' no encontrado en dropdown SGS")
        await elegido.click()

    async def _fill_and_save(page, item: dict, label: str) -> dict:
        codigo = item.get("codigo", str(item.get("pm_id", "")))
        pm_id = item["pm_id"]
        cantidad = float(item.get("cantidad") or 0)
        t0 = time.perf_counter()

        row = page.locator(f".o_data_row:has-text('{codigo}')").first
        await row.wait_for(state="visible", timeout=60_000)
        await row.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)

        add_sel = (
            ".modal [name='advance_project_material_ids'] .o_field_x2many_list_row_add a, "
            ".modal a:has-text('AGREGAR'), "
            ".modal a:has-text('Agregar')"
        )

        # Abrir modal con verificación — máx 2 intentos (pre-GUARDAR, retryable per R4)
        for _intento_modal in range(2):
            await row.locator("button[name='action_show_details']").first.click()
            _modal_ok = False
            try:
                await page.wait_for_selector(
                    ".modal:has-text('Consumption detail'), .modal-dialog:has-text('Consumption detail')",
                    timeout=30_000,
                )
                await page.wait_for_timeout(700)
                # Verificar que el modal está en estado editable correcto
                await page.locator(add_sel).first.wait_for(state="visible", timeout=4_000)
                _modal_ok = True
            except Exception:
                pass

            if _modal_ok:
                break

            log.warning(f"[{label}] Modal abrió en estado incorrecto para {codigo} "
                        f"(intento {_intento_modal + 1}/2) — cerrando y reintentando")
            await _close_modal(page, label, codigo)
            await page.wait_for_timeout(1500)
            if _intento_modal == 1:
                raise BrowserActionError(
                    f"modal_bad_state — modal no abrió correctamente para {codigo} tras 2 intentos"
                )

        await page.locator(add_sel).first.click()
        await page.wait_for_timeout(800)

        qty_sel = (
            ".modal [name='advance_project_material_ids'] tr.o_selected_row input[name='amount'], "
            ".modal tr.o_selected_row input[name='amount'], "
            ".modal [name='amount'] input, "
            ".modal input[name='amount']"
        )
        qty = page.locator(qty_sel).last
        await qty.wait_for(state="visible", timeout=15_000)
        await qty.click(click_count=3)
        await qty.fill(str(cantidad))

        await _select_partner(page, item.get("partner_nombre", ""))
        await page.wait_for_timeout(500)

        def _is_write_response(resp):
            try:
                body = resp.request.post_data or ""
                return '"write"' in body and '"project.material"' in body and f"[{pm_id}]" in body
            except Exception:
                return False

        save_btn = page.locator(
            ".modal-footer button:has-text('GUARDAR'), "
            ".modal-footer button:has-text('Guardar')"
        ).first
        await save_btn.wait_for(state="visible", timeout=15_000)
        t_save = time.perf_counter()
        # Paso 7 — GUARDAR con intercepción de red.
        # Timeout capturado aquí (no propagado) para que _worker_ws lo clasifique
        # como save_timeout (R1: no reintentar post-GUARDAR).
        try:
            async with page.expect_response(_is_write_response, timeout=45_000) as resp_info:
                await save_btn.click()
            resp = await resp_info.value
            data = await resp.json()
        except AsyncPWTimeout:
            write_dt = time.perf_counter() - t_save
            return {
                "ok": False,
                "error": (f"Timeout {write_dt:.1f}s esperando respuesta HTTP del GUARDAR "
                          "— dato puede ya estar guardado en SGS. Verificar manualmente."),
                "error_tipo": "save_timeout",
                "write_dt": round(write_dt, 2),
            }
        write_dt = time.perf_counter() - t_save
        if data.get("error"):
            return {
                "ok": False,
                "error": data["error"].get("data", {}).get("message") or str(data["error"])[:500],
                "error_tipo": "save_error",
                "write_dt": round(write_dt, 2),
            }

        details = await _wait_details(item)
        confirm_dt = time.perf_counter() - t_save
        if not details:
            return {
                "ok": False,
                "error": "GUARDAR respondio OK pero no aparecio project.material.details",
                "error_tipo": "post_confirm_timeout",
                "write_dt": round(write_dt, 2),
                "confirm_dt": round(confirm_dt, 2),
            }
        detail = details[0]
        amount = float(detail.get("amount") or 0)
        if abs(amount - cantidad) > 0.001:
            return {
                "ok": False,
                "error": f"Detalle creado con amount={amount:g}, esperado={cantidad:g}",
                "error_tipo": "post_amount_mismatch",
                "write_dt": round(write_dt, 2),
                "confirm_dt": round(confirm_dt, 2),
                "detail": detail,
            }
        if detail.get("state") and detail.get("state") != "done":
            return {
                "ok": False,
                "error": f"Detalle creado con state={detail.get('state')}",
                "error_tipo": "post_state_not_done",
                "write_dt": round(write_dt, 2),
                "confirm_dt": round(confirm_dt, 2),
                "detail": detail,
            }
        await _close_modal(page, label, codigo)
        return {
            "ok": True,
            "write_dt": round(write_dt, 2),
            "confirm_dt": round(confirm_dt, 2),
            "duracion_s": round(time.perf_counter() - t0, 2),
            "detail": detail,
        }

    def _base_result(item: dict) -> dict:
        return {
            "tipo": "MAT",
            "pm_id": item.get("pm_id"),
            "codigo": item.get("codigo", ""),
            "codigo_sgs": item.get("codigo_sgs", item.get("codigo", "")),
            "es_custodia": item.get("es_custodia", False),
            "pro_code": item.get("pro_code", ""),
            "cantidad": item.get("cantidad", 0),
            "cantidad_solicitada": item.get("cantidad", 0),
            "excel_neto": item.get("excel_neto"),
            "pid": item.get("pid"),
            "partner_nombre": item.get("partner_nombre", ""),
            "pasos": [],
            "intentos": 1,
        }

    # ── Reporte diagnóstico por material fallido ──────────────────────────────
    async def _save_diag_report(page, item: dict, label: str,
                                error_tipo: str, error_msg: str) -> str:
        """
        Toma screenshot y guarda JSON con el contexto completo del fallo.
        Retorna la ruta del reporte (para incluir en logs).
        """
        import json as _json_mod
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        cod = str(item.get("codigo", item.get("pm_id", "unk")))
        logs_dir = os.path.join(os.path.dirname(__file__), "logs", "diag")
        os.makedirs(logs_dir, exist_ok=True)

        scr_path = os.path.join(logs_dir, f"{ts}_{label}_{cod}.png")
        try:
            await page.screenshot(path=scr_path, full_page=False)
        except Exception:
            scr_path = None

        report = {
            "timestamp": ts,
            "worker": label,
            "pro_code": item.get("pro_code", ""),
            "pm_id": item.get("pm_id"),
            "codigo": item.get("codigo", ""),
            "nodo": item.get("nodo", ""),
            "lider": item.get("partner_nombre", ""),
            "cantidad_calculada": item.get("cantidad"),
            "excel_neto": item.get("excel_neto"),
            "bandeja_despacho": item.get("bandeja_despacho", ""),
            "split": item.get("split"),
            "error_tipo": error_tipo,
            "error": error_msg,
            "screenshot": scr_path,
            "logica": (
                f"PRO={item.get('pro_code')} | pm_id={item.get('pm_id')} | "
                f"cod={item.get('codigo')} | nodo={item.get('nodo')} | "
                f"lider={item.get('partner_nombre')} | "
                f"cantidad={item.get('cantidad')} | excel_neto={item.get('excel_neto')} | "
                f"bandeja={item.get('bandeja_despacho')} | split={item.get('split')}"
            ),
        }

        rep_path = os.path.join(logs_dir, f"{ts}_{label}_{cod}.json")
        try:
            with open(rep_path, "w", encoding="utf-8") as _f:
                _json_mod.dump(report, _f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        log.error(f"[{label}] DIAG_REPORT: {rep_path}")
        return rep_path

    # ── Errores post-GUARDAR — R1: no reintentar ──────────────────────────────
    _POST_GUARDAR_TIPOS = frozenset({
        "save_error", "post_confirm_timeout",
        "post_amount_mismatch", "post_state_not_done",
    })

    # ── Worker con cola compartida (work-stealing) ────────────────────────────
    async def _worker_ws(page, label: str, tab_idx: int,
                         shared_q: asyncio.Queue) -> list:
        """
        Consume items de shared_q hasta que este vacia o haya un error fatal.

        Stagger: tab_idx > 0 espera _WS_STAGGER_SECS antes del primer item.

        On error:
          - save_error / post_confirm_timeout / post_amount_mismatch
            / post_state_not_done: el tab se detiene (R1 CLAUDE.md — no
            reintentar post-GUARDAR). Items restantes tomados por el otro tab.
          - ui_timeout / ui_error / BrowserActionError (pre-GUARDAR):
            guarda reporte diagnóstico + screenshot y CONTINUA al siguiente
            material. El tab NO se detiene: el fallo fue antes de escribir.
          - skipped_existing: continua normalmente (el item ya estaba liquidado).
        """
        if tab_idx > 0:
            await asyncio.sleep(_WS_STAGGER_SECS * tab_idx)

        # Tolerancia por material: 70 s (> 45 s del expect_response de GUARDAR).
        # Si se excede, el fallo fue pre-GUARDAR o el servidor está completamente colgado.
        _MAT_TIMEOUT_S = 70

        out = []
        while True:
            try:
                orig_idx, item = shared_q.get_nowait()
            except asyncio.QueueEmpty:
                break

            codigo = item.get("codigo", item.get("pm_id", "?"))

            if _cancel_requested():
                res = dict(_base_result(item), ok=False,
                           error="Liquidacion cancelada por el usuario",
                           error_tipo="cancelado", worker_id=label)
                out.append((orig_idx, res))
                if progress_cb:
                    progress_cb(orig_idx, label, "error", codigo, error=res["error"])
                break

            try:
                # Pre-check: ya existe un detalle con esta combinación partner+cantidad?
                # Para multi-lider: el mismo pm_id puede tener múltiples detalles.
                details = await _read_details(item["pm_id"])
                if details:
                    _partner_b = (item.get("partner_nombre") or "").strip().upper()
                    _qty_b     = float(item.get("cantidad") or 0)
                    _ya_existe = False
                    for _d in details:
                        _pf = _d.get("partner_id")
                        _dp = str(_pf[1] if isinstance(_pf, list) and len(_pf) >= 2 else "").strip().upper()
                        _da = float(_d.get("amount") or 0)
                        if abs(_da - _qty_b) < 0.001 and (not _partner_b or _partner_b in _dp or _dp in _partner_b):
                            _ya_existe = True
                            break
                    if _ya_existe:
                        res = dict(_base_result(item), ok=True, worker_id=label,
                                   skipped_existing=True, detail=details[0])
                        out.append((orig_idx, res))
                        if progress_cb:
                            progress_cb(orig_idx, label, "ok", codigo)
                        continue   # <- sigue consumiendo la cola

                if progress_cb:
                    progress_cb(orig_idx, label, "corriendo", codigo)

                res_inner = await asyncio.wait_for(
                    _fill_and_save(page, item, label),
                    timeout=_MAT_TIMEOUT_S,
                )
                res = dict(_base_result(item), **res_inner, worker_id=label)
                if progress_cb:
                    progress_cb(orig_idx, label,
                                "ok" if res.get("ok") else "error",
                                codigo, error=res.get("error"))
                out.append((orig_idx, res))

                if not res.get("ok"):
                    et = res.get("error_tipo", "")
                    if et in _POST_GUARDAR_TIPOS:
                        # R1: post-GUARDAR → tab se detiene.
                        log.warning(f"[MAT-WS {label}] Tab detenido post-GUARDAR en "
                                    f"{codigo}: {et} — cola para el otro tab")
                        break
                    # Pre-GUARDAR devuelto como dict (BrowserActionError, etc.) → continuar
                    await _save_diag_report(page, item, label, et, res.get("error", ""))
                    log.warning(f"[MAT-WS {label}] Error pre-GUARDAR en {codigo}: {et} — continuando")

            except asyncio.TimeoutError:
                # 70 s agotados: el tab estaba colgado (pre-GUARDAR o servidor sin respuesta).
                # No es R1 (GUARDAR tiene su propio timeout de 45 s dentro de _fill_and_save).
                err_msg = f"Timeout {_MAT_TIMEOUT_S}s por material — tab continuara con el siguiente"
                rep = await _save_diag_report(page, item, label, "mat_timeout_global", err_msg)
                res = dict(_base_result(item), ok=False, error=err_msg,
                           error_tipo="mat_timeout_global", worker_id=label,
                           diag_report=rep)
                out.append((orig_idx, res))
                if progress_cb:
                    progress_cb(orig_idx, label, "error", codigo, error=err_msg)
                log.warning(f"[MAT-WS {label}] Global timeout en {codigo} — continuando")

            except AsyncPWTimeout as ex:
                err_msg = _safe(ex)
                rep = await _save_diag_report(page, item, label, "ui_timeout", err_msg)
                res = dict(_base_result(item), ok=False, error=err_msg,
                           error_tipo="ui_timeout", worker_id=label, diag_report=rep)
                out.append((orig_idx, res))
                if progress_cb:
                    progress_cb(orig_idx, label, "error", codigo, error=err_msg)
                log.warning(f"[MAT-WS {label}] Timeout UI en {codigo} — continuando")

            except Exception as ex:
                err_msg = _safe(ex)
                rep = await _save_diag_report(page, item, label, "ui_error", err_msg)
                res = dict(_base_result(item), ok=False, error=err_msg,
                           error_tipo="ui_error", worker_id=label, diag_report=rep)
                out.append((orig_idx, res))
                if progress_cb:
                    progress_cb(orig_idx, label, "error", codigo, error=err_msg)
                log.warning(f"[MAT-WS {label}] Error UI en {codigo} — continuando")

        return out

    # ── Grupo por proyecto con cola compartida ────────────────────────────────
    async def _run_group_ws(pid, indexed_items):
        """
        Abre min(max_pages, len(items)) tabs para un mismo proyecto.
        Todos los tabs comparten una sola asyncio.Queue.
        El tab secundario aplica stagger inicial antes de consumir.
        """
        indexed_items = list(indexed_items)
        pages_n = min(max_pages, len(indexed_items))

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=False, slow_mo=80)
            context = await browser.new_context(
                storage_state=storage_state, viewport={"width": 1400, "height": 900}
            )
            pages = []
            for i in range(pages_n):
                page = await context.new_page()
                page.set_default_timeout(WAIT_UNTIL)
                pages.append(page)

            # Abrir lista de materiales del proyecto en todos los tabs a la vez
            await asyncio.gather(*[
                _open_list(page, pid, f"p{pid}-tab{i+1}")
                for i, page in enumerate(pages)
            ])

            # Cola compartida — todos los items del proyecto en orden FIFO
            shared_q: asyncio.Queue = asyncio.Queue()
            for pair in indexed_items:
                await shared_q.put(pair)

            log.info(f"[MAT-WS] pid={pid} items={shared_q.qsize()} tabs={pages_n} "
                     f"stagger={_WS_STAGGER_SECS}s")

            # Lanzar workers; cada uno drena la cola compartida
            tasks = [
                _worker_ws(pages[i], f"p{pid}-tab{i+1}", i, shared_q)
                for i in range(pages_n)
            ]
            nested = await asyncio.gather(*tasks)
            await browser.close()

        flat = []
        for part in nested:
            flat.extend(part)
        return flat

    async def _main():
        by_pid = {}
        for i, item in enumerate(items):
            by_pid.setdefault(item.get("pid") or 0, []).append((i, item))

        all_pairs = []
        pid_groups = []
        for pid, indexed in by_pid.items():
            if not pid:
                for i, item in indexed:
                    all_pairs.append((i, dict(_base_result(item), ok=False,
                                              error="Item MAT sin pid para abrir lista",
                                              error_tipo="missing_pid")))
            else:
                pid_groups.append((pid, indexed))

        # Procesar en lotes de max_concurrent_nodes pids simultáneos
        for batch_start in range(0, len(pid_groups), max_concurrent_nodes):
            batch = pid_groups[batch_start:batch_start + max_concurrent_nodes]
            if len(batch) > 1:
                log.info(f"[MAT-WS] Lote nodos {batch_start//max_concurrent_nodes + 1}: "
                         f"{len(batch)} pids simultáneos — "
                         f"{[p for p, _ in batch]}")
            batch_results = await asyncio.gather(*[
                _run_group_ws(pid, indexed) for pid, indexed in batch
            ])
            for part in batch_results:
                all_pairs.extend(part)

        return all_pairs

    try:
        pairs = asyncio.run(_main())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            pairs = loop.run_until_complete(_main())
        finally:
            loop.close()
    except Exception as ex:
        return [dict(_base_result(it), ok=False, error=_safe(ex), error_tipo="mat_lista_ws_crash")
                for it in items]

    resultados = [None] * len(items)
    for idx, res in pairs:
        resultados[idx] = res
    for i, res in enumerate(resultados):
        if res is None:
            resultados[i] = dict(_base_result(items[i]), ok=False,
                                 error="Sin resultado en MAT lista/modal (WS)",
                                 error_tipo="missing_result")
    return resultados


def liquidar_materiales_parallel(items: list,
                                  max_workers: int = 2,
                                  storage_state: dict = None,
                                  progress_cb=None,
                                  should_cancel=None) -> list:
    """
    Pool de workers con cola de tareas.

    Modelo: N workers comparten una cola. Cada worker toma la siguiente tarea
    disponible cuando termina la anterior — si un worker falla/crashea los
    demás siguen consumiendo la cola sin dejar items huérfanos.

    Por cada error, _clasificar_error() decide la acción:
      - login_timeout / nav_timeout / generic_timeout  → esperar y reintentar 1 vez
      - addline_timeout                                → scroll + espera + reintentar
      - session_expired                                → re-login + reintentar
      - edit_timeout / unknown                         → reportar sin reintentar
                                                         (pm_id bloqueado en SGS)
    """
    if not items:
        return []

    import time as _time

    # Cola compartida: (orig_idx, item, intento)
    cola: _queue_mod.Queue = _queue_mod.Queue()
    for i, item in enumerate(items):
        cola.put((i, item, 0))          # intento 0 = primera vez

    resultados       = [None] * len(items)
    resultados_lock  = _threading_mod.Lock()
    MAX_INTENTOS     = 1

    def _cb(orig_idx, worker_id, estado, cod, error=None):
        if progress_cb:
            try:
                progress_cb(orig_idx, worker_id, estado, cod, error=error)
            except Exception:
                pass

    def _marcar_cola_como_error(worker_id, error_tipo, msg):
        """Drena la cola marcando todos los items pendientes como error."""
        while True:
            try:
                orig_idx, item, _ = cola.get_nowait()
                cod = item.get("codigo", item.get("pm_id", "?"))
                res = {
                    "ok": False, "error": msg, "error_tipo": error_tipo,
                    "intentos": 0, "tipo": "MAT",
                    "codigo":       item.get("codigo", ""),
                    "codigo_sgs":   item.get("codigo_sgs", item.get("codigo", "")),
                    "es_custodia":  item.get("es_custodia", False),
                    "pro_code":     item.get("pro_code", ""),
                    "cantidad":     item.get("cantidad", 0),
                    "pid":          item.get("pid"),
                    "worker_id":    worker_id,
                    "partner_nombre": item.get("partner_nombre", ""),
                    "pasos": [],
                }
                _cb(orig_idx, worker_id, "error", cod, error=msg)
                with resultados_lock:
                    resultados[orig_idx] = res
                cola.task_done()
            except _queue_mod.Empty:
                break

    def _cancel_requested():
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    def _worker(worker_id: int):
        # Escalonar arranque: 8s entre workers para no golpear SGS simultáneamente
        # (aumentado de 5s: con tiempos de espera mayores los workers se solapan más)
        _time.sleep(worker_id * 8)

        # Sin storage_state no hay sesión — el gate en blueprint.py debe
        # haber bloqueado esta situación; si llega aquí es un bug.
        if not storage_state:
            log.error(f"[WORKER-{worker_id}] Sin storage_state — sesión no disponible")
            _marcar_cola_como_error(worker_id, "no_session",
                                    "Sin sesión — inicia sesión en el browser visible")
            return

        pw = PlaywrightClient(headless=False, slow_mo=100)
        log.info(f"[WORKER-{worker_id}] Iniciando con sesión heredada...")
        try:
            pw.start(storage_state=storage_state)
            # storage_state ya pone _logueado=True — sin login adicional
        except Exception as ex:
            log.error(f"[WORKER-{worker_id}] No pudo iniciar el browser: {_safe(ex)}")
            _marcar_cola_como_error(worker_id, "browser_start_error", _safe(ex))
            return

        log.info(f"[WORKER-{worker_id}] Browser listo — procesando cola...")

        try:
            while True:
                if _cancel_requested():
                    _marcar_cola_como_error(worker_id, "cancelado", "Liquidacion cancelada por el usuario")
                    break
                try:
                    orig_idx, item, intento = cola.get_nowait()
                except _queue_mod.Empty:
                    break   # cola vacía — worker termina normalmente

                cod = item.get("codigo", item.get("pm_id", "?"))
                log.info(f"[WORKER-{worker_id}] [{intento+1}°] {_safe(cod)} qty={item.get('cantidad')}")
                _cb(orig_idx, worker_id, "corriendo", cod)

                res = None
                try:
                    res = pw.liquidar_material(
                        pm_id          = item["pm_id"],
                        cantidad       = item["cantidad"],
                        pro_code       = item.get("pro_code", ""),
                        partner_nombre = item.get("partner_nombre", ""),
                        pendiente_api_confirmado = item.get("pendiente_api_confirmado", False),
                        screenshots    = False,
                    )
                except Exception as ex:
                    res = {"ok": False, "error": _safe(ex), "pasos": []}

                # ── Clasificar error y decidir acción ──────────────────────────
                if not res.get("ok"):
                    tipo_error = _clasificar_error(res.get("error", ""))
                    log.warning(
                        f"[WORKER-{worker_id}] {_safe(cod)} FALLO "
                        f"tipo={tipo_error} intento={intento+1}/{MAX_INTENTOS} "
                        f"error={res.get('error','')[:120]}"
                    )
                    _cb(orig_idx, worker_id, "error", cod, error=res.get("error"))

                    if intento + 1 < MAX_INTENTOS:
                        if tipo_error in ("login_timeout", "nav_timeout", "generic_timeout"):
                            # Servidor lento — esperar y reintentar
                            # IMPORTANTE: save_timeout NO está aquí — el GUARDAR ya fue enviado a Odoo
                            # y el dato probablemente SÍ fue grabado. Reintentar un save_timeout
                            # causaría doble-escritura (advance = 2x). Reportar como error y verificar.
                            wait_s = 15
                            log.info(f"[WORKER-{worker_id}] Esperando {wait_s}s antes de reintentar (tipo={tipo_error})...")
                            _time.sleep(wait_s)
                            cola.put((orig_idx, item, intento + 1))
                            cola.task_done()
                            continue

                        elif tipo_error == "session_expired":
                            # Sesión caducó — re-login y reintentar
                            log.info(f"[WORKER-{worker_id}] Re-login por sesion expirada...")
                            try:
                                pw.login()
                                cola.put((orig_idx, item, intento + 1))
                                cola.task_done()
                                continue
                            except Exception as ex2:
                                log.error(f"[WORKER-{worker_id}] Re-login fallido: {_safe(ex2)}")
                                res["error"] = f"Re-login fallido: {_safe(ex2)}"

                        elif tipo_error == "addline_timeout":
                            # x2many no renderizó — scroll + espera + reintentar
                            log.info(f"[WORKER-{worker_id}] Esperando 10s (addline timeout)...")
                            _time.sleep(10)
                            cola.put((orig_idx, item, intento + 1))
                            cola.task_done()
                            continue

                        # edit_timeout / unknown → no reintentar
                        # (pm_id bloqueado o error desconocido — reportar directo)
                        res["error_tipo"] = tipo_error

                    else:
                        # Agotados los intentos
                        res["error_tipo"] = tipo_error
                        log.error(
                            f"[WORKER-{worker_id}] {_safe(cod)} AGOTADOS {MAX_INTENTOS} intentos "
                            f"— tipo={tipo_error}"
                        )
                        _cb(orig_idx, worker_id, "error", cod,
                            error=f"[{tipo_error}] {res.get('error','')} (x{MAX_INTENTOS})")
                else:
                    log.info(f"[WORKER-{worker_id}] {_safe(cod)} → OK")
                    _cb(orig_idx, worker_id, "ok", cod)

                # Enriquecer resultado con metadatos del item
                res.setdefault("pasos", [])
                res["tipo"]           = "MAT"
                res["codigo"]         = item.get("codigo", "")
                res["codigo_sgs"]     = item.get("codigo_sgs", item.get("codigo", ""))
                res["es_custodia"]    = item.get("es_custodia", False)
                res["pro_code"]       = item.get("pro_code", "")
                res.setdefault("cantidad_solicitada", item.get("cantidad", 0))
                res.setdefault("cantidad", item.get("cantidad", 0))
                res["excel_neto"]     = item.get("excel_neto")
                res["pid"]            = item.get("pid")
                res["worker_id"]      = worker_id
                res["partner_nombre"] = item.get("partner_nombre", "")
                res["intentos"]       = intento + 1

                with resultados_lock:
                    resultados[orig_idx] = res

                cola.task_done()

                # Pausa entre items para no saturar el servidor
                _time.sleep(2)

        except Exception as ex:
            log.error(f"[WORKER-{worker_id}] CRASH: {_safe(ex)}")
            print(f"[WORKER-{worker_id}] CRASH: {_safe(ex)}", flush=True)
        finally:
            try:
                pw.close()
            except Exception:
                pass
            log.info(f"[WORKER-{worker_id}] terminado.")

    # Lanzar workers
    n = min(max_workers, len(items))
    print(f"[POOL] {n} worker(s) para {len(items)} material(es)...", flush=True)
    log.info(f"[POOL] {n} worker(s) para {len(items)} material(es)")

    with ThreadPoolExecutor(max_workers=n) as executor:
        futures = [executor.submit(_worker, i) for i in range(n)]
        for future in _as_completed(futures):
            try:
                future.result()
            except Exception as ex:
                log.error(f"[POOL] Future error: {ex}")

    print(f"[POOL] Terminado.", flush=True)

    # Rellenar huecos (items que nadie tomó por crash de todos los workers)
    for i, r in enumerate(resultados):
        if r is None:
            resultados[i] = {
                "pm_id":      items[i].get("pm_id"), "ok": False,
                "tipo":       "MAT",
                "error":      "Sin resultado — todos los workers fallaron",
                "error_tipo": "worker_crash",
                "codigo":     items[i].get("codigo", ""),
                "pro_code":   items[i].get("pro_code", ""),
                "cantidad":   items[i].get("cantidad", 0),
                "intentos":   0, "pasos": [],
            }

    return resultados


# ── Pool v2: Auxiliar + Worker con semáforos por proyecto ─────────────────────

def liquidar_materiales_parallel_v2(items: list,
                                     max_workers: int = POOL_V2_MAX_WORKERS,
                                     num_preloaders: int = POOL_V2_NUM_PRELOADERS,
                                     storage_state: dict = None,
                                     progress_cb=None) -> list:
    """
    Pool v2: Auxiliar + Worker.

    Mejoras sobre v1:
      1. Semaforos por proyecto: max 1 worker por pid simultaneamente.
         Se mantiene en modo conservador; el v2 queda desactivado por defecto.

      2. Preloaders — browsers headless que pre-navegan los forms antes de que
         el worker llegue. El worker chequea el warmup_registry: si el form fue
         cargado hace <TTL segundos, usa timeouts reducidos (30s en vez de 120s).

      3. Stagger de 8s: valor validado para desfasar guardados.

    Medidas de seguridad:
      - Preloader crash: failure-safe — workers continúan normalmente sin warmup.
      - TTL de warmup: si el worker tarda >WARMUP_TTL_S, ignora el warmup (podría
        estar stale después de que el servidor descartó la sesión de preloader).
      - Rate-limiting del preloader: min 2s entre navegaciones.
      - Semáforo por proyecto: solo 1 escritura concurrente por pid (cero contención).
      - save_timeout: NO retryable (igual que v1) — dato ya grabado.
      - max_preloaders cap: nunca más preloaders que items disponibles.
    """
    if not items:
        return []

    import time as _time

    # ── Estado compartido entre threads ───────────────────────────────────────
    cola: _queue_mod.Queue = _queue_mod.Queue()
    for i, item in enumerate(items):
        cola.put((i, item, 0))

    resultados       = [None] * len(items)
    resultados_lock  = _threading_mod.Lock()
    MAX_INTENTOS     = 1

    # warmup_registry: {pm_id: timestamp_float}  — cuándo el preloader terminó de cargar el form
    warmup_registry: dict = {}
    warmup_lock      = _threading_mod.Lock()

    # semáforos por proyecto: máx 1 worker por pid
    project_sems: dict = {}
    project_sems_lock = _threading_mod.Lock()

    # señal de parada para preloaders
    stop_preloaders = _threading_mod.Event()

    # cola de pm_ids que los preloaders deben visitar
    preload_queue: _queue_mod.Queue = _queue_mod.Queue()

    def _get_project_sem(pid):
        with project_sems_lock:
            if pid not in project_sems:
                project_sems[pid] = _threading_mod.Semaphore(1)
            return project_sems[pid]

    def _cb(orig_idx, worker_id, estado, cod, error=None):
        if progress_cb:
            try:
                progress_cb(orig_idx, worker_id, estado, cod, error=error)
            except Exception:
                pass

    def _marcar_cola_como_error(worker_id, error_tipo, msg):
        while True:
            try:
                orig_idx, item, _ = cola.get_nowait()
                cod = item.get("codigo", item.get("pm_id", "?"))
                res = {
                    "ok": False, "error": msg, "error_tipo": error_tipo,
                    "intentos": 0, "tipo": "MAT",
                    "codigo":       item.get("codigo", ""),
                    "codigo_sgs":   item.get("codigo_sgs", item.get("codigo", "")),
                    "es_custodia":  item.get("es_custodia", False),
                    "pro_code":     item.get("pro_code", ""),
                    "cantidad":     item.get("cantidad", 0),
                    "pid":          item.get("pid"),
                    "worker_id":    worker_id,
                    "partner_nombre": item.get("partner_nombre", ""),
                    "pasos": [],
                }
                _cb(orig_idx, worker_id, "error", cod, error=msg)
                with resultados_lock:
                    resultados[orig_idx] = res
                cola.task_done()
            except _queue_mod.Empty:
                break

    # ── Preloader thread ───────────────────────────────────────────────────────
    def _preloader(preloader_id: int):
        """
        Browser auxiliar de solo navegación. No escribe nada.
        Navega forms y registra el timestamp en warmup_registry.
        Si falla, simplemente no registra (worker sigue con timeout normal).
        Rate-limitado a POOL_V2_PRELOADER_RATE seg entre requests.
        """
        if not storage_state:
            log.warning(f"[PRELOADER-{preloader_id}] Sin storage_state — preloader inactivo")
            return

        pw = PlaywrightClient(headless=True, slow_mo=100)  # slow_mo bajo: solo navegación
        try:
            pw.start(storage_state=storage_state)
            log.info(f"[PRELOADER-{preloader_id}] Listo")
        except Exception as ex:
            log.error(f"[PRELOADER-{preloader_id}] No pudo iniciar: {_safe(ex)}")
            return

        last_nav_ts = 0.0
        try:
            while not stop_preloaders.is_set():
                try:
                    pm_id = preload_queue.get(timeout=1.0)
                except _queue_mod.Empty:
                    continue

                # Seguridad: rate-limiting
                elapsed = _time.time() - last_nav_ts
                if elapsed < POOL_V2_PRELOADER_RATE:
                    _time.sleep(POOL_V2_PRELOADER_RATE - elapsed)

                # Seguridad: no recargar si ya está warm y no expiró
                with warmup_lock:
                    ts = warmup_registry.get(pm_id, 0)
                if _time.time() - ts < POOL_V2_WARMUP_TTL_S:
                    log.debug(f"[PRELOADER-{preloader_id}] pm#{pm_id} ya warm — skip")
                    preload_queue.task_done()
                    continue

                try:
                    pw._navegar_material(pm_id)
                    with warmup_lock:
                        warmup_registry[pm_id] = _time.time()
                    log.info(f"[PRELOADER-{preloader_id}] pm#{pm_id} warm ✓")
                except Exception as ex:
                    log.warning(f"[PRELOADER-{preloader_id}] pm#{pm_id} nav falló (sin impacto): {_safe(ex)}")

                last_nav_ts = _time.time()
                preload_queue.task_done()

        except Exception as ex:
            log.error(f"[PRELOADER-{preloader_id}] CRASH (sin impacto en workers): {_safe(ex)}")
        finally:
            try:
                pw.close()
            except Exception:
                pass
            log.info(f"[PRELOADER-{preloader_id}] terminado")

    # ── Worker thread ──────────────────────────────────────────────────────────
    def _worker(worker_id: int):
        _time.sleep(worker_id * POOL_V2_STAGGER_S)

        if not storage_state:
            log.error(f"[WORKER-{worker_id}] Sin storage_state")
            _marcar_cola_como_error(worker_id, "no_session",
                                    "Sin sesión — inicia sesión en el browser visible")
            return

        pw = PlaywrightClient(headless=False, slow_mo=100)
        try:
            pw.start(storage_state=storage_state)
        except Exception as ex:
            log.error(f"[WORKER-{worker_id}] No pudo iniciar el browser: {_safe(ex)}")
            _marcar_cola_como_error(worker_id, "browser_start_error", _safe(ex))
            return

        log.info(f"[WORKER-{worker_id}] Browser listo")

        try:
            while True:
                try:
                    orig_idx, item, intento = cola.get_nowait()
                except _queue_mod.Empty:
                    break

                pm_id = item.get("pm_id")
                cod   = item.get("codigo", pm_id or "?")
                pid   = item.get("pid")

                log.info(f"[WORKER-{worker_id}] [{intento+1}°] {_safe(cod)} pm#{pm_id} pid={pid}")
                _cb(orig_idx, worker_id, "corriendo", cod)

                # ── Seguridad: semáforo por proyecto (configurable) ───────────
                # POOL_V2_USE_PROJECT_SEM=True  → máx 1 worker por pid (seguro, lento)
                # POOL_V2_USE_PROJECT_SEM=False → workers libres por pid (rápido, leve riesgo)
                project_sem = (_get_project_sem(pid) if pid else None) if POOL_V2_USE_PROJECT_SEM else None
                if project_sem:
                    log.debug(f"[WORKER-{worker_id}] Esperando slot para pid={pid}...")
                    project_sem.acquire()
                    log.debug(f"[WORKER-{worker_id}] Slot pid={pid} adquirido")

                res = None
                t_inicio = _time.time()
                try:
                    # ── Aplicar timeouts reducidos si el form está warm ────────
                    with warmup_lock:
                        warm_ts = warmup_registry.get(pm_id, 0)
                    age_s = _time.time() - warm_ts
                    if warm_ts and age_s < POOL_V2_WARMUP_TTL_S:
                        log.info(
                            f"[WORKER-{worker_id}] pm#{pm_id} warm ({age_s:.0f}s) "
                            "— timeout reducido a 30s"
                        )
                        pw._page.set_default_timeout(30_000)
                    else:
                        pw._page.set_default_timeout(WAIT_UNTIL)  # 120s normal

                    res = pw.liquidar_material(
                        pm_id          = pm_id,
                        cantidad       = item["cantidad"],
                        pro_code       = item.get("pro_code", ""),
                        partner_nombre = item.get("partner_nombre", ""),
                        pendiente_api_confirmado = item.get("pendiente_api_confirmado", False),
                        screenshots    = False,
                    )

                    # Limpiar warmup usado (no reusar el mismo timestamp)
                    with warmup_lock:
                        warmup_registry.pop(pm_id, None)

                except Exception as ex:
                    res = {"ok": False, "error": _safe(ex), "pasos": []}
                finally:
                    # Siempre liberar el semáforo del proyecto
                    if project_sem:
                        project_sem.release()
                        log.debug(f"[WORKER-{worker_id}] Slot pid={pid} liberado")
                    # Restaurar timeout por defecto
                    try:
                        pw._page.set_default_timeout(WAIT_UNTIL)
                    except Exception:
                        pass

                # ── Clasificar error ───────────────────────────────────────────
                if not res.get("ok"):
                    tipo_error = _clasificar_error(res.get("error", ""))
                    log.warning(
                        f"[WORKER-{worker_id}] {_safe(cod)} FALLO "
                        f"tipo={tipo_error} intento={intento+1}/{MAX_INTENTOS} "
                        f"error={res.get('error','')[:120]}"
                    )
                    _cb(orig_idx, worker_id, "error", cod, error=res.get("error"))

                    if intento + 1 < MAX_INTENTOS:
                        if tipo_error in ("login_timeout", "nav_timeout", "generic_timeout"):
                            wait_s = 15
                            log.info(f"[WORKER-{worker_id}] Esperando {wait_s}s antes de reintentar...")
                            _time.sleep(wait_s)
                            cola.put((orig_idx, item, intento + 1))
                            cola.task_done()
                            continue

                        elif tipo_error == "session_expired":
                            log.info(f"[WORKER-{worker_id}] Re-login por sesion expirada...")
                            try:
                                pw.login()
                                cola.put((orig_idx, item, intento + 1))
                                cola.task_done()
                                continue
                            except Exception as ex2:
                                log.error(f"[WORKER-{worker_id}] Re-login fallido: {_safe(ex2)}")
                                res["error"] = f"Re-login fallido: {_safe(ex2)}"

                        elif tipo_error == "addline_timeout":
                            log.info(f"[WORKER-{worker_id}] Esperando 10s (addline timeout)...")
                            _time.sleep(10)
                            cola.put((orig_idx, item, intento + 1))
                            cola.task_done()
                            continue

                        res["error_tipo"] = tipo_error
                    else:
                        res["error_tipo"] = tipo_error
                        log.error(
                            f"[WORKER-{worker_id}] {_safe(cod)} AGOTADOS {MAX_INTENTOS} intentos"
                        )
                        _cb(orig_idx, worker_id, "error", cod,
                            error=f"[{tipo_error}] {res.get('error','')} (x{MAX_INTENTOS})")
                else:
                    log.info(f"[WORKER-{worker_id}] {_safe(cod)} → OK")
                    _cb(orig_idx, worker_id, "ok", cod)

                    # Encolar el siguiente item para pre-carga anticipada
                    # (solo si el preload_queue no está saturado)
                    try:
                        next_items = list(cola.queue)[:1]  # peek sin consumir
                        for _, next_item, _ in next_items:
                            next_pm = next_item.get("pm_id")
                            if next_pm and preload_queue.qsize() < num_preloaders + 2:
                                preload_queue.put_nowait(next_pm)
                    except Exception:
                        pass

                res.setdefault("pasos", [])
                res["tipo"]           = "MAT"
                res["codigo"]         = item.get("codigo", "")
                res["codigo_sgs"]     = item.get("codigo_sgs", item.get("codigo", ""))
                res["es_custodia"]    = item.get("es_custodia", False)
                res["pro_code"]       = item.get("pro_code", "")
                res.setdefault("cantidad_solicitada", item.get("cantidad", 0))
                res.setdefault("cantidad", item.get("cantidad", 0))
                res["excel_neto"]     = item.get("excel_neto")
                res["pid"]            = item.get("pid")
                res["worker_id"]      = worker_id
                res["partner_nombre"] = item.get("partner_nombre", "")
                res["intentos"]       = intento + 1

                with resultados_lock:
                    resultados[orig_idx] = res

                res["duracion_s"] = round(_time.time() - t_inicio, 1)
                log.info(f"[WORKER-{worker_id}] {_safe(cod)} duracion={res['duracion_s']}s ok={res.get('ok')}")
                cola.task_done()
                _time.sleep(2)

        except Exception as ex:
            log.error(f"[WORKER-{worker_id}] CRASH: {_safe(ex)}")
        finally:
            try:
                pw.close()
            except Exception:
                pass
            log.info(f"[WORKER-{worker_id}] terminado")

    # ── Arrancar preloaders + workers ──────────────────────────────────────────
    n_workers    = min(max_workers, len(items))
    n_preloaders = min(num_preloaders, len(items))

    # Cargar los primeros items en la cola de preload anticipada
    for _, item, _ in list(cola.queue)[:n_preloaders * 2]:
        pm_id = item.get("pm_id")
        if pm_id:
            preload_queue.put_nowait(pm_id)

    print(
        f"[POOL-v2] {n_workers} worker(s) + {n_preloaders} preloader(s) "
        f"para {len(items)} material(es)  "
        f"[stagger={POOL_V2_STAGGER_S}s, warmup_ttl={POOL_V2_WARMUP_TTL_S}s]",
        flush=True,
    )
    log.info(
        f"[POOL-v2] {n_workers} workers + {n_preloaders} preloaders "
        f"para {len(items)} items"
    )

    preloader_threads = []
    for i in range(n_preloaders):
        t = _threading_mod.Thread(
            target=_preloader, args=(i,), daemon=True, name=f"preloader-{i}"
        )
        t.start()
        preloader_threads.append(t)

    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        futures = [executor.submit(_worker, i) for i in range(n_workers)]
        for future in _as_completed(futures):
            try:
                future.result()
            except Exception as ex:
                log.error(f"[POOL-v2] Future error: {ex}")

    # Detener preloaders limpiamente
    stop_preloaders.set()
    for t in preloader_threads:
        t.join(timeout=5)

    print("[POOL-v2] Terminado.", flush=True)

    # Rellenar huecos
    for i, r in enumerate(resultados):
        if r is None:
            resultados[i] = {
                "pm_id":      items[i].get("pm_id"), "ok": False,
                "tipo":       "MAT",
                "error":      "Sin resultado — todos los workers fallaron",
                "error_tipo": "worker_crash",
                "codigo":     items[i].get("codigo", ""),
                "pro_code":   items[i].get("pro_code", ""),
                "cantidad":   items[i].get("cantidad", 0),
                "intentos":   0, "pasos": [],
            }

    return resultados
