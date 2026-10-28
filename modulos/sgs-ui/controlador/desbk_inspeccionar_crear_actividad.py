"""
Inspecciona el formulario de CREACIÓN de actividad en PRO-052970.
No escribe nada — solo vuelca campos y toma screenshots.
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))
from playwright.sync_api import sync_playwright

URL  = "https://cofe.sgsdominion-global.com"
DB   = "DOMINION_CO_002"
PID  = 50193   # PRO-052970
LOGS = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS, exist_ok=True)

def ss(page, n):
    p = os.path.join(LOGS, f"crea_{n}.png")
    page.screenshot(path=p, full_page=False)
    print(f"  [ss] crea_{n}.png")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=500)
    page    = browser.new_page(viewport={"width":1400,"height":900})
    page.set_default_timeout(25_000)

    # Login
    page.goto(f"{URL}/web/login?db={DB}", wait_until="domcontentloaded")
    page.fill("input[name='login']",    "1051064568")
    page.fill("input[name='password']", "1051064568")
    page.click("button[type='submit']")
    page.wait_for_selector(".o_main_navbar", timeout=25_000)
    print("[OK] Login")

    # Navegar al proyecto
    page.goto(
        f"{URL}/web#id={PID}&action=485&model=project.dominion&view_type=form&cids=1&menu_id=352",
        wait_until="domcontentloaded"
    )
    page.wait_for_selector(".o_form_view", timeout=20_000)
    time.sleep(2)

    # Abrir actividades
    page.locator("[name='action_button_activities']").first.click()
    time.sleep(2.5)
    for f in page.locator(".o_searchview .o_facet_remove").all():
        try: f.click(); time.sleep(0.3)
        except: pass
    time.sleep(1)
    ss(page, "03_lista_actividades")

    # Click CREAR
    crear_btn = page.locator("button:has-text('CREAR')").first
    if crear_btn.count() > 0 and crear_btn.is_visible(timeout=3000):
        print("[OK] Click CREAR")
        crear_btn.click()
        time.sleep(2.5)
        ss(page, "04_form_nueva_actividad")

        # Volcar campos
        all_names = set()
        for el in page.locator("[name]").all():
            try:
                n = el.get_attribute("name")
                if n: all_names.add(n)
            except: pass
        print(f"\nCampos en form nueva actividad:")
        for n in sorted(all_names):
            # Intentar leer valor actual
            el = page.locator(f"[name='{n}']").first
            try:    val = el.input_value()
            except:
                try: val = el.inner_text().strip()[:60]
                except: val = ""
            print(f"  [{n}] = '{val}'")

        # HTML completo
        html = page.locator(".o_form_view").first.inner_html()
        with open(os.path.join(LOGS, "form_nueva_act.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nHTML guardado ({len(html)} chars): logs/form_nueva_act.html")

        ss(page, "05_campos_detalle")

        # Descartar
        page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')").first.click()
        time.sleep(1)
        print("[OK] Descartado — nada guardado")
    else:
        print("[!!] Boton CREAR no encontrado")
        ss(page, "04_sin_crear")

    print("\nCerrando en 5s...")
    time.sleep(5)
    browser.close()
