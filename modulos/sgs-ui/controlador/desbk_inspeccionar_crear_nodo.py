"""
inspeccionar_crear_nodo.py — Inspecciona el formulario de CREACIÓN de proyecto/nodo en SGS.
No escribe nada — solo vuelca campos, valores y toma screenshots.

Objetivo: capturar qué campos existen en el form de nuevo project.dominion
para implementar la creación automatizada de nodos faltantes.

Instrucciones:
  1. Ejecutar: python inspeccionar_crear_nodo.py
  2. Revisar salida en consola (campos + valores)
  3. Revisar screenshots en logs/ y HTML en logs/form_nuevo_nodo.html
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))
from playwright.sync_api import sync_playwright

URL  = "https://cofe.sgsdominion-global.com"
DB   = "DOMINION_CO_002"
LOGS = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS, exist_ok=True)

# ── Proyecto de referencia: 6300998574 (tiene nodos conocidos) ────────────────
# Se usa para navegar al contexto correcto antes de crear un nuevo nodo.
# El PID_PADRE es el project.dominion del proyecto padre (ajustar si hace falta).
# Dejar en None para ir directo a la lista de proyectos y crear desde ahí.
PID_PADRE = None   # ej: 50193

def ss(page, n):
    p = os.path.join(LOGS, f"nodo_{n}.png")
    page.screenshot(path=p, full_page=False)
    print(f"  [ss] nodo_{n}.png")

def volcar_campos(page, label=""):
    all_names = {}
    for el in page.locator("[name]").all():
        try:
            n = el.get_attribute("name")
            if not n:
                continue
            try:    val = el.input_value()
            except:
                try: val = el.inner_text().strip()[:80]
                except: val = ""
            all_names[n] = val
        except:
            pass
    print(f"\n── Campos en form {label} ──")
    for n, v in sorted(all_names.items()):
        print(f"  [{n}] = '{v}'")
    return all_names

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=400)
    page    = browser.new_page(viewport={"width":1440,"height":900})
    page.set_default_timeout(25_000)

    # ── Login ─────────────────────────────────────────────────────────────────
    page.goto(f"{URL}/web/login?db={DB}", wait_until="domcontentloaded")
    page.fill("input[name='login']",    "1051064568")
    page.fill("input[name='password']", "1051064568")
    page.click("button[type='submit']")
    page.wait_for_selector(".o_main_navbar", timeout=25_000)
    print("[OK] Login")

    # ── Estrategia A: ir a la lista de project.dominion y crear nuevo ─────────
    # menu_id=352 es el menú de proyectos verificado en producción.
    print("\n[1] Navegando a lista de proyectos (project.dominion)...")
    page.goto(
        f"{URL}/web?cids=1&menu_id=352#action=project.dominion&view_type=list",
        wait_until="domcontentloaded"
    )
    time.sleep(3)
    ss(page, "01_lista_proyectos")

    # Intentar NUEVO desde la lista
    nuevo_btn = page.locator("button:has-text('NUEVO'), button:has-text('Nuevo'), a:has-text('Nuevo')")
    if nuevo_btn.count() > 0 and nuevo_btn.first.is_visible(timeout=5000):
        print("[OK] Click NUEVO desde lista")
        nuevo_btn.first.click()
        time.sleep(3)
        ss(page, "02_form_nuevo_nodo_desde_lista")
        campos_lista = volcar_campos(page, "NUEVO desde lista")

        html = page.locator(".o_form_view").first.inner_html()
        with open(os.path.join(LOGS, "form_nuevo_nodo_lista.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML guardado: logs/form_nuevo_nodo_lista.html ({len(html)} chars)")

        # Descartar
        desc = page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')")
        if desc.count() > 0:
            desc.first.click()
            time.sleep(1)
            print("[OK] Descartado")
    else:
        print("[!!] Botón NUEVO no encontrado en lista")
        ss(page, "02_sin_nuevo")

    # ── Estrategia B: si hay PID_PADRE, ir al proyecto padre y buscar "Agregar nodo" ──
    if PID_PADRE:
        print(f"\n[2] Navegando al proyecto padre pid={PID_PADRE}...")
        page.goto(
            f"{URL}/web?cids=1&menu_id=352#id={PID_PADRE}&action=485&model=project.dominion&view_type=form",
            wait_until="domcontentloaded"
        )
        page.wait_for_selector(".o_form_view", timeout=20_000)
        time.sleep(2)
        ss(page, "03_form_padre")

        # Listar todos los botones visibles
        print("\nBotones visibles en form padre:")
        for btn in page.locator("button:visible, a.btn:visible").all():
            try:
                txt = btn.inner_text().strip()
                if txt:
                    print(f"  [BTN] '{txt}'")
            except:
                pass

        # Listar smart buttons
        print("\nSmart buttons (stat buttons):")
        for sb in page.locator(".o_stat_button").all():
            try:
                txt = sb.inner_text().strip().replace("\n", " ")
                name = sb.get_attribute("name") or ""
                print(f"  [STAT] name='{name}' texto='{txt[:60]}'")
            except:
                pass

        ss(page, "04_botones_padre")

    # ── Estrategia C: URL directa de nuevo registro ───────────────────────────
    print("\n[3] Intentando URL de nuevo registro (sin ID)...")
    page.goto(
        f"{URL}/web?cids=1&menu_id=352#action=485&model=project.dominion&view_type=form",
        wait_until="domcontentloaded"
    )
    time.sleep(3)
    ss(page, "05_form_nuevo_url_directo")

    try:
        page.wait_for_selector(".o_form_view", timeout=10_000)
        campos_url = volcar_campos(page, "URL directo nuevo")
        html = page.locator(".o_form_view").first.inner_html()
        with open(os.path.join(LOGS, "form_nuevo_nodo_url.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML guardado: logs/form_nuevo_nodo_url.html ({len(html)} chars)")

        desc = page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')")
        if desc.count() > 0:
            desc.first.click()
            time.sleep(1)
            print("[OK] Descartado")
    except Exception as ex:
        print(f"[!!] Form no cargó con URL directa: {ex}")

    print("\n[DONE] Cerrando en 5s — revisar logs/ para screenshots y HTML")
    time.sleep(5)
    browser.close()
