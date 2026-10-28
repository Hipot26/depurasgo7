"""Inspecciona el formulario project.material id=513498 — SIN escribir nada."""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))
from playwright.sync_api import sync_playwright

URL  = "https://cofe.sgsdominion-global.com"
DB   = "DOMINION_CO_002"
PM_ID = 513498
LOGS = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS, exist_ok=True)

def ss(page, n):
    p = os.path.join(LOGS, f"mat_{n}.png")
    page.screenshot(path=p, full_page=False)
    print(f"  [ss] mat_{n}.png")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=400)
    page    = browser.new_page(viewport={"width":1400,"height":900})
    page.set_default_timeout(25_000)

    page.goto(f"{URL}/web/login?db={DB}", wait_until="domcontentloaded")
    page.fill("input[name='login']",    "1051064568")
    page.fill("input[name='password']", "1051064568")
    page.click("button[type='submit']")
    page.wait_for_selector(".o_main_navbar")
    print("[OK] Login")

    # Navegar directo al project.material
    page.goto(
        f"{URL}/web#id={PM_ID}&model=project.material&view_type=form&cids=1&menu_id=352",
        wait_until="domcontentloaded"
    )
    page.wait_for_selector(".o_form_view", timeout=20_000)
    time.sleep(2)
    ss(page, "01_form_lectura")

    # Campos en modo lectura
    print("\nCampos visibles (lectura):")
    for el in page.locator("[name]").all():
        try:
            n = el.get_attribute("name")
            if not n: continue
            try:    v = el.input_value()
            except: v = el.inner_text().strip()[:50]
            print(f"  [{n}] = '{v}'")
        except: pass

    # Entrar en edición
    edit_btn = page.locator("button:has-text('EDITAR'), button:has-text('Editar')").first
    if edit_btn.count() > 0 and edit_btn.is_visible(timeout=3000):
        edit_btn.click()
        time.sleep(1.5)
        print("\n[OK] Modo edicion")
    ss(page, "02_edit")

    # Campo advance_project_material_ids
    campo = page.locator("[name='advance_project_material_ids']")
    if campo.count() > 0:
        print(f"\n[OK] advance_project_material_ids encontrado")
        html = campo.inner_html()
        with open(os.path.join(LOGS,"mat_field.html"),"w",encoding="utf-8") as f: f.write(html)
        print(f"  HTML guardado ({len(html)} chars)")
    else:
        print("\n[!!] advance_project_material_ids NO encontrado")

    # Botón agregar línea
    add = page.locator(
        "[name='advance_project_material_ids'] td.o_field_x2many_list_row_add a, "
        "[name='advance_project_material_ids'] .o_field_x2many_list_row_add a"
    ).first
    print(f"\nBoton agregar linea: count={add.count()}")
    if add.count() > 0:
        print(f"  texto='{add.inner_text().strip()}'")
        add.click()
        time.sleep(1.2)
        ss(page, "03_nueva_fila")

        # Campos en la nueva fila
        print("\nCampos en la nueva fila (tr.o_selected_row):")
        fila = page.locator("tr.o_selected_row")
        if fila.count() > 0:
            for el in fila.locator("[name]").all():
                try:
                    n = el.get_attribute("name")
                    if not n: continue
                    # Tag type
                    tag = el.evaluate("e => e.tagName")
                    # Tiene input dentro?
                    inp = el.locator("input").first
                    tiene_input = inp.count() > 0
                    print(f"  [{n}] tag={tag} tiene_input={tiene_input}")
                except: pass
        ss(page, "04_fila_campos")

    # Descartar
    page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')").first.click()
    time.sleep(1)
    print("\n[OK] Descartado — nada guardado")

    time.sleep(4)
    browser.close()
print("Listo. Ver logs/mat_*.png")
