"""
inspeccionar_mo_list.py
Crea una actividad temporal (SIN GUARDAR) en la lista de PRO-053161
para ver los botones de la fila y el button_group_0/1.
"""
import os, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(__file__))
from playwright.sync_api import sync_playwright

URL  = "https://cofe.sgsdominion-global.com"
DB   = "DOMINION_CO_002"
PID  = 50376   # PRO-053161 NODO 4
LOGS = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOGS, exist_ok=True)

_n = 0
def ss(page, n):
    global _n; _n += 1
    p = os.path.join(LOGS, f"mo_insp_{_n:02d}_{n}.png")
    page.screenshot(path=p, full_page=False)
    print(f"  [ss] {os.path.basename(p)}")

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=False, slow_mo=300)
    page    = browser.new_page(viewport={"width":1440,"height":900})
    page.set_default_timeout(30_000)

    # Login
    page.goto(f"{URL}/web/login?db={DB}", wait_until="domcontentloaded", timeout=60_000)
    time.sleep(1)
    page.fill("input[name='login']", "1051064568")
    page.fill("input[name='password']", "1051064568")
    page.click("button[type='submit']")
    page.wait_for_selector(".o_main_navbar, .o_action_manager", timeout=60_000)
    print("[OK] Login")

    # Double-goto al proyecto
    url_proy = f"{URL}/web?cids=1&menu_id=352#id={PID}&action=485&model=project.dominion&view_type=form"
    page.goto(f"{URL}/web?cids=1", wait_until="domcontentloaded"); time.sleep(0.5)
    page.goto(url_proy, wait_until="domcontentloaded")
    page.wait_for_selector(".o_form_view", timeout=30_000); time.sleep(2)
    print("[OK] Proyecto")
    ss(page, "proyecto")

    # Abrir lista de actividades
    page.locator("button[name='action_button_activities']").first.click()
    time.sleep(2.5)
    for f in page.locator(".o_searchview .o_facet_remove").all():
        try: f.click(); time.sleep(0.2)
        except: pass
    time.sleep(1)
    ss(page, "lista_vacia")
    print("[OK] Lista de actividades abierta")

    # Si ya hay filas, inspeccionar directamente
    rows_existentes = page.locator(".o_data_row").count()
    print(f"Filas existentes: {rows_existentes}")

    if rows_existentes > 0:
        print("=== Inspeccionando fila existente ===")
        fila = page.locator(".o_data_row").first
        fila.hover(); time.sleep(0.8)
        ss(page, "hover_fila_existente")

        # Todos los botones de la fila
        btns = fila.locator("button, a.btn").all()
        print(f"Botones en fila: {len(btns)}")
        for b in btns:
            try:
                name = b.get_attribute("name") or ""
                txt  = b.inner_text().strip()[:40]
                cls  = (b.get_attribute("class") or "")[:60]
                vis  = b.is_visible(timeout=500)
                print(f"  name='{name}' txt='{txt}' visible={vis} class='{cls[:40]}'")
            except: pass

        # Botones en button_group_0 y button_group_1
        for grp in ["button_group_0", "button_group_1"]:
            btns_grp = fila.locator(f"td[name='{grp}'] button").all()
            print(f"\n  Botones en {grp}: {len(btns_grp)}")
            for b in btns_grp:
                try:
                    name = b.get_attribute("name") or ""
                    txt  = b.inner_text().strip()[:40]
                    cls  = (b.get_attribute("class") or "")[:60]
                    print(f"    name='{name}' txt='{txt}'")
                except: pass

        # Click en la fila para abrir el form
        fila.click(); time.sleep(2)
        ss(page, "form_desde_click_fila")

    else:
        # Crear fila temporal para ver botones (SIN GUARDAR)
        print("=== Creando fila temporal (SIN guardar) para ver botones ===")
        crear_btn = page.locator("button:has-text('CREAR'), button:has-text('Crear')").first
        if crear_btn.count() == 0 or not crear_btn.is_visible(timeout=3000):
            print("[!!] Boton CREAR no encontrado")
            ss(page, "sin_crear")
        else:
            crear_btn.click(); time.sleep(2)
            ss(page, "fila_nueva_creada")
            print("[OK] Fila nueva aparecio")

            # Ver estructura de la fila nueva
            row_nueva = page.locator(".o_data_row.o_selected_row, .o_data_row tr.o_selected_row, tr.o_selected_row").first
            all_rows = page.locator(".o_data_row").all()
            print(f"Filas totales ahora: {len(all_rows)}")

            # Volcar HTML de toda la lista con la fila visible
            html_lista = page.locator(".o_list_view, .o_list_renderer").first.inner_html()
            with open(os.path.join(LOGS, "mo_lista_con_fila.html"), "w", encoding="utf-8") as f:
                f.write(html_lista)
            print(f"HTML lista con fila guardado ({len(html_lista)} chars)")

            # Ver botones en todas las filas
            for i, row in enumerate(all_rows[:3]):
                btns = row.locator("button, a.btn, td button").all()
                print(f"  Fila[{i}] botones: {len(btns)}")
                for b in btns:
                    try:
                        name = b.get_attribute("name") or ""
                        txt  = b.inner_text().strip()[:40]
                        vis  = b.is_visible(timeout=300)
                        print(f"    name='{name}' txt='{txt}' vis={vis}")
                    except: pass

                # Botones por columna
                for grp in ["button_group_0", "button_group_1"]:
                    btns_grp = row.locator(f"td[data-name='{grp}'] button, td.o_list_button button").all()
                    print(f"    button_group [{grp}]: {len(btns_grp)} btns")
                    for b in btns_grp:
                        try:
                            name = b.get_attribute("name") or ""
                            txt  = b.inner_text().strip()[:40]
                            print(f"      name='{name}' txt='{txt}'")
                        except: pass

            # Llenar activities_id para que la fila quede "completa"
            act_input = page.locator("tr.o_selected_row [name='activities_id'] input").first
            if act_input.count() > 0 and act_input.is_visible(timeout=2000):
                act_input.click()
                act_input.press_sequentially("QP", delay=80)
                time.sleep(1.5)
                ss(page, "dropdown_actividad")
                # No seleccionar — solo ver
                page.keyboard.press("Escape")
                time.sleep(0.5)

            # DESCARTAR (sin guardar)
            print("[..] Descartando sin guardar...")
            descartar = page.locator("button:has-text('DESCARTAR'), button:has-text('Descartar')").first
            if descartar.count() > 0 and descartar.is_visible(timeout=3000):
                descartar.click(); time.sleep(1)

                # Si pide confirmación
                conf = page.locator(".modal button:has-text('DESCARTAR'), .modal button:has-text('Descartar'), .modal button:has-text('No guardar')").first
                if conf.count() > 0 and conf.is_visible(timeout=2000):
                    conf.click(); time.sleep(0.5)
            print("[OK] Descartado — nada guardado")

    # Inspeccionar también un form de actividad existente (act_id=20)
    print("\n=== Form de project.activities id=20 (existente en BD) ===")
    url_act = f"{URL}/web?cids=1&menu_id=352#id=20&model=project.activities&view_type=form"
    page.goto(f"{URL}/web?cids=1", wait_until="domcontentloaded"); time.sleep(0.3)
    page.goto(url_act, wait_until="domcontentloaded")
    page.wait_for_selector(".o_form_view", timeout=20_000); time.sleep(2)
    ss(page, "form_act_20")

    # Botones control panel y statusbar
    all_btns = page.locator(".o_control_panel button, .o_statusbar button").all()
    print(f"Botones control panel/statusbar: {len(all_btns)}")
    for b in all_btns:
        try:
            name = b.get_attribute("name") or ""
            txt  = b.inner_text().strip()[:50]
            print(f"  name='{name}' txt='{txt}'")
        except: pass

    # Leer los action items del action menu (engranaje)
    cog = page.locator(".o_cp_action_menus .o_dropdown_toggler, .o_cp_action_menus button").first
    if cog.count() > 0 and cog.is_visible(timeout=2000):
        cog.click(); time.sleep(0.8)
        ss(page, "action_menu")
        items = page.locator(".o_dropdown_item, .o_menu_item").all()
        print(f"Items en action menu: {len(items)}")
        for it in items:
            try: print(f"  '{it.inner_text().strip()}'")
            except: pass
        page.keyboard.press("Escape")

    print("\nInspeccion completada. Cerrando en 10s...")
    time.sleep(10)
    browser.close()
