"""Abre SGS en el navegador y navega al proyecto para inspeccion visual."""
import os, time
from playwright.sync_api import sync_playwright

URL = "https://cofe.sgsdominion-global.com"
DB  = "DOMINION_CO_002"
LOGS = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS, exist_ok=True)

def ss(page, n):
    p = os.path.join(LOGS, f"{n}.png")
    page.screenshot(path=p, full_page=False)
    print(f"  >> {n}.png")
    return p

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=400)
    page    = browser.new_page(viewport={"width":1400,"height":900})
    page.set_default_timeout(25_000)

    # Login
    print("Login...")
    page.goto(f"{URL}/web/login?db={DB}", wait_until="domcontentloaded")
    page.wait_for_selector("input[name='login']")
    page.fill("input[name='login']",    "1051064568")
    page.fill("input[name='password']", "1051064568")
    page.click("button[type='submit']")
    page.wait_for_selector(".o_main_navbar", timeout=25_000)
    print("Login OK")
    ss(page, "open_01_home")

    # Navegar al proyecto PRO-052970
    print("Navegando a PRO-052970...")
    page.goto(
        f"{URL}/web#id=50193&action=485&model=project.dominion&view_type=form&cids=1&menu_id=352",
        wait_until="domcontentloaded"
    )
    page.wait_for_selector(".o_form_view", timeout=20_000)
    time.sleep(3)
    ss(page, "open_02_proyecto")

    # Leer todos los stat buttons (name=action_button_*)
    print("\nStat buttons encontrados:")
    for btn in page.locator("[name^='action_button']").all():
        try:
            n   = btn.get_attribute("name")
            txt = btn.inner_text().strip().replace("\n"," ")
            print(f"  [{n}] '{txt}'")
        except Exception:
            pass

    # Click en action_button_activities
    print("\nClick en action_button_activities...")
    page.locator("[name='action_button_activities']").first.click()
    time.sleep(3)
    ss(page, "open_03_actividades")
    print(f"URL actual: {page.url}")

    # Quitar filtro si hay
    filtros = page.locator(".o_searchview .o_facet_remove").all()
    if filtros:
        print(f"Quitando {len(filtros)} filtro(s)...")
        for f in filtros:
            try: f.click(); time.sleep(0.4)
            except: pass
        time.sleep(1.5)
    ss(page, "open_04_sin_filtro")

    # Ver botones disponibles en la vista
    print("\nBotones en la vista de actividades:")
    for btn in page.locator("button, .o_list_buttons button").all():
        try:
            txt = btn.inner_text().strip()
            if txt: print(f"  '{txt}'")
        except: pass

    print("\nNavegando ahora a PRO-052153 (tiene actividades conocidas)...")
    # Buscar PRO-052153 via readonly client para obtener su id
    # (del historial sabemos que PRO-052153 = project.dominion con code PRO-052153)
    page.goto(
        f"{URL}/web#model=project.dominion&view_type=list&cids=1&menu_id=352",
        wait_until="domcontentloaded"
    )
    time.sleep(2)
    # Buscar en la barra de búsqueda
    page.fill(".o_searchview input", "PRO-052153")
    page.keyboard.press("Enter")
    time.sleep(2)
    ss(page, "open_05_busqueda")

    # Click en el primer resultado
    fila = page.locator("tr.o_data_row").first
    if fila.count() > 0:
        print(f"  Resultado: '{fila.inner_text().strip()[:80]}'")
        fila.click()
        time.sleep(2)
        ss(page, "open_06_pro052153")

        # Click en actividades
        page.locator("[name='action_button_activities']").first.click()
        time.sleep(3)

        # Quitar filtros
        for f in page.locator(".o_searchview .o_facet_remove").all():
            try: f.click(); time.sleep(0.4)
            except: pass
        time.sleep(1.5)
        ss(page, "open_07_actividades_052153")

        print("\nFilas en PRO-052153:")
        for i, fila in enumerate(page.locator("tr.o_data_row").all()):
            try: print(f"  [{i}] {fila.inner_text().strip()[:100]}")
            except: pass
    else:
        print("  Sin resultados para PRO-052153")

    print("\nCerrando en 6 segundos...")
    time.sleep(6)
    browser.close()

print("Listo. Ver logs/open_*.png")
