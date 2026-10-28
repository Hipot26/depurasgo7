# -*- coding: utf-8 -*-
"""
sharepoint_dl.py — Descarga la planilla PRESUPUESTO desde el link de
SharePoint/OneDrive que viene en los correos.

Cómo funciona:
  - Navegador Playwright con PERFIL PERSISTENTE propio (data/sp_profile): la
    sesión de Microsoft queda guardada ahí igual que en un navegador normal
    (~1 día, según la política de Dominion).
  - Si la sesión está viva → entra al link, ubica el archivo cuyo nombre
    empieza por "PRESUPUESTO" y lo descarga.
  - Si la sesión expiró → devuelve "login_requerido"; el Centro MO muestra el
    botón de inicio de sesión que abre una ventana real para que el operador entre a mano
    (la máquina nunca conoce ni guarda la contraseña de Microsoft).
"""
import os
import re
import time
import subprocess

DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(DIR_SCRIPT, "data", "sp_profile")
os.makedirs(PROFILE, exist_ok=True)

URL_BASE = "https://dominionglobal.sharepoint.com"


def _es_login(url: str) -> bool:
    return any(s in (url or "") for s in
               ("login.microsoftonline", "login.live", "login.srf", "/_login"))


_MARCA_LOGIN = os.path.join(PROFILE, ".login_ok")

# Ventana "invisible": Chromium COMPLETO (comparte cookies con la ventana de
# login) posicionado fuera de la pantalla. El headless clásico usa otro binario
# con su PROPIO almacén de cookies — por eso nunca veía la sesión (bug real
# depurado 2026-08-14 con capturas).
_ARGS_FUERA_PANTALLA = ["--window-position=-32000,-32000"]


def _cerrar_instancias_perfil():
    """Chromium bloquea el perfil si otra instancia lo tiene abierto (p. ej.
    una ventana de login que quedó abierta) — cerrarlas antes de continuar."""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-WmiObject Win32_Process -Filter \"name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -like '*sp_profile*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
            capture_output=True, timeout=30)
        time.sleep(1.5)
    except Exception:
        pass

_BANNER_JS = """
(() => {
  const d = document.createElement('div');
  d.textContent = '✓ Sesión guardada para el Centro MO — ya puedes CERRAR esta ventana.';
  d.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;' +
    'background:#188a4c;color:#fff;font:600 16px Segoe UI,sans-serif;' +
    'padding:14px 20px;text-align:center;box-shadow:0 2px 8px rgba(0,0,0,.3)';
  document.body.appendChild(d);
})()
"""


def _marcar_login_ok():
    try:
        with open(_MARCA_LOGIN, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def login_reciente(horas: float = 20.0) -> bool:
    """True si hubo un login interactivo exitoso hace menos de `horas`."""
    try:
        with open(_MARCA_LOGIN) as f:
            return (time.time() - float(f.read().strip())) < horas * 3600
    except Exception:
        return False


def login_interactivo(url: str = URL_BASE, espera_s: int = 600) -> bool:
    """Abre una ventana real para que el operador inicie sesión.

    IMPORTANTE (bug corregido 2026-08-13): la ventana NO se cierra sola al
    detectar el login — Microsoft escribe las cookies persistentes durante y
    después de la pregunta "¿Mantener la sesión iniciada?" (responder SÍ).
    Cuando el login aterriza en SharePoint se muestra un aviso verde y la
    ventana queda abierta hasta que el operador la cierre; recién ahí se cierra el
    contexto y el perfil queda con la sesión asentada."""
    from playwright.sync_api import sync_playwright
    _cerrar_instancias_perfil()      # una ventana vieja bloquearía el perfil
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            PROFILE, headless=False, accept_downloads=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(url, timeout=60_000)
        except Exception:
            pass
        fin = time.time() + espera_s
        ok = False
        while time.time() < fin:
            try:
                u = page.url
            except Exception:
                break                      # el operador cerró la ventana antes de entrar
            if "sharepoint.com" in u and not _es_login(u):
                ok = True
                break
            try:
                page.wait_for_timeout(1000)
            except Exception:
                break

        if ok:
            # Asentar la sesión: recargar la base, avisar, y esperar a que
            # el operador cierre la ventana (máximo 10 min) — nunca cerrarla solos.
            try:
                page.wait_for_timeout(4000)
                page.evaluate(_BANNER_JS)
            except Exception:
                pass
            _marcar_login_ok()
            fin2 = time.time() + 600
            while time.time() < fin2:
                try:
                    _ = page.url
                    page.wait_for_timeout(800)
                except Exception:
                    break                  # ventana cerrada por el operador
        try:
            ctx.close()
        except Exception:
            pass
        return ok


def _atravesar_login(page, espera_s: int = 30) -> bool:
    """La sesión de Entra (cuenta Microsoft) dura días, pero la cookie propia
    de SharePoint expira a diario: al volver, Microsoft no pide contraseña,
    solo muestra "Selección de la cuenta" con la cuenta recordada. Un humano
    da UN clic; esto hace ese mismo clic (y responde Sí a "¿Mantener la
    sesión iniciada?"). Solo se rinde si aparece el campo de contraseña.
    Devuelve True si aterrizó en SharePoint."""
    fin = time.time() + espera_s
    while time.time() < fin:
        try:
            u = page.url
        except Exception:
            return False
        if "sharepoint.com" in u and not _es_login(u):
            return True
        try:
            # Contraseña pedida de verdad → hace falta login manual
            pwd = page.locator("input[type='password']")
            if pwd.count() and pwd.first.is_visible():
                return False
            # "¿Mantener la sesión iniciada?" → Sí (id clásico de Azure AD)
            btn_si = page.locator("#idSIButton9")
            if btn_si.count() and btn_si.first.is_visible():
                btn_si.first.click()
                page.wait_for_timeout(2000)
                continue
            # Selector de cuenta → clic en la cuenta recordada
            tile = page.get_by_text(re.compile(r"@dominion-global\.com", re.I)).first
            if tile.count() and tile.is_visible():
                tile.click()
                page.wait_for_timeout(2500)
                continue
        except Exception:
            pass
        try:
            page.wait_for_timeout(1000)
        except Exception:
            return False
    try:
        return "sharepoint.com" in page.url and not _es_login(page.url)
    except Exception:
        return False


def descargar_presupuesto(url: str, destino_dir: str,
                          patron: str = "PRESUPUESTO", timeout_s: int = 120,
                          headless: bool = True):
    """Entra al link y descarga el primer archivo que calce con `patron`.
    Devuelve (ruta_local, None) o (None, "login_requerido"|mensaje de error).

    Siempre usa el Chromium COMPLETO con la ventana fuera de la pantalla (el
    parámetro headless se ignora): es la única forma de compartir la sesión
    del login. Si el perfil está bloqueado por una ventana vieja, la cierra y
    reintenta."""
    from playwright.sync_api import sync_playwright
    os.makedirs(destino_dir, exist_ok=True)
    rx = re.compile(patron, re.IGNORECASE)

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(
                PROFILE, headless=False, accept_downloads=True,
                args=_ARGS_FUERA_PANTALLA)
        except Exception:
            _cerrar_instancias_perfil()
            try:
                ctx = pw.chromium.launch_persistent_context(
                    PROFILE, headless=False, accept_downloads=True,
                    args=_ARGS_FUERA_PANTALLA)
            except Exception as e:
                return None, f"No se pudo abrir el navegador (perfil bloqueado): {str(e)[:100]}"
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6000)             # el SPA de SharePoint tarda en pintar
            if _es_login(page.url):
                # Intentar el SSO silencioso (clic en la cuenta recordada)
                if not _atravesar_login(page):
                    return None, "login_requerido"
                page.wait_for_timeout(5000)         # el deep-link recarga tras el SSO

            # Celda de nombre en la biblioteca moderna de SharePoint
            celda = page.locator('[data-automationid="FieldRenderer-name"]',
                                 has_text=rx).first
            try:
                celda.wait_for(timeout=20_000)
            except Exception:
                celda = page.get_by_text(rx).first
                try:
                    celda.wait_for(timeout=8_000)
                except Exception:
                    if _es_login(page.url):
                        return None, "login_requerido"
                    return None, (f"No se encontró ningún archivo '{patron}' en la "
                                  f"página del link (¿la carpeta cambió?)")

            # Intento 1: clic derecho sobre el archivo → menú → Descargar
            try:
                celda.click(button="right", timeout=10_000)
                with page.expect_download(timeout=timeout_s * 1000) as dl_info:
                    page.locator('[data-automationid="downloadCommand"]').first.click(timeout=8_000)
                dl = dl_info.value
            except Exception:
                # Intento 2: seleccionar la fila y usar la barra de comandos
                fila = celda.locator('xpath=ancestor::*[@data-automationid="DetailsRow"][1]')
                fila.hover()
                fila.locator('[data-automationid="row-selection-checkbox"], [role="checkbox"]').first.click(timeout=8_000)
                with page.expect_download(timeout=timeout_s * 1000) as dl_info:
                    page.locator('[data-automationid="downloadCommand"]').first.click(timeout=10_000)
                dl = dl_info.value

            nombre = dl.suggested_filename or "PRESUPUESTO.xlsx"
            ruta = os.path.join(destino_dir, nombre)
            dl.save_as(ruta)
            return ruta, None
        except Exception as e:
            return None, f"{type(e).__name__}: {str(e)[:140]}"
        finally:
            try:
                ctx.close()
            except Exception:
                pass
