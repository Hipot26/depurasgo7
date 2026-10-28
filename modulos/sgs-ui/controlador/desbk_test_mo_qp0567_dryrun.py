"""
test_mo_qp0567_dryrun.py
========================
Demuestra que FECHA_FIJA_MO="30/04/2026" se escribe en el modal MO.

Flujo:
  1. Navega a PRO-053619
  2. CREA QP0567 (saves el registro de actividad — paso necesario para tener la fila)
  3. Abre modal "Avance del proyecto" via action_show_details
  4. AGREGAR LÍNEA → llena cantidad + fecha (30/04/2026) + partner VILLAMIL
  5. Screenshot → DESCARTA sin guardar el avance

Lo que escribe en SGS: solo el registro de actividad QP0567 (proyectado=1.0).
Lo que NO escribe: ningún avance (se descarta el modal).
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sgs_readonly_client import SGSReadOnlyClient
from browser_client import PlaywrightClient, FECHA_FIJA_MO, BrowserActionError
from playwright.sync_api import TimeoutError as PWTimeout

PID      = 50797
PRO_CODE = "PRO-053622"
ACT_CODE = "QP0567"
CANTIDAD = 1.0
PARTNER  = "VILLAMIL . LUIS RAUL"
CEDULA   = "6300998576"

print(f"\n{'='*60}")
print(f"  DRY RUN MODAL — {PRO_CODE} / {ACT_CODE}")
print(f"  Fecha esperada: {FECHA_FIJA_MO}")
print(f"{'='*60}\n")

c = SGSReadOnlyClient('https://cofe.sgsdominion-global.com','DOMINION_CO_002','1051064568','1051064568')
c.autenticar()

pw = PlaywrightClient(headless=False, slow_mo=150)
pw.start()
pw.login()
print("Login OK\n")

page = pw._page

try:
    # ── 1. Navegar al proyecto ────────────────────────────────────────────────
    print(f"[1] Navegando a {PRO_CODE}...")
    pw._navegar_proyecto(PID, pro_code=PRO_CODE)
    pw._ss("qp0567_01_proy")

    # ── 2. Abrir lista de actividades ─────────────────────────────────────────
    print("[2] Abriendo actividades...")
    page.locator("[name='action_button_activities']").first.click()
    time.sleep(2.0)
    for f in page.locator(".o_searchview .o_facet_remove").all():
        try: f.click(); time.sleep(0.3)
        except: pass
    time.sleep(1.0)
    pw._ss("qp0567_02_lista")

    # ── 3. Ver si QP0567 ya existe en la lista ────────────────────────────────
    fila_existente = page.locator(f".o_data_row:has-text('{ACT_CODE}')").first
    ya_existe = fila_existente.count() > 0
    try:
        ya_existe = ya_existe and fila_existente.is_visible(timeout=2000)
    except PWTimeout:
        ya_existe = False

    if not ya_existe:
        # ── 3b. CREAR QP0567 ──────────────────────────────────────────────────
        print(f"[3] Creando actividad {ACT_CODE} (proyectado={CANTIDAD})...")
        crear_btn = page.locator("button:has-text('CREAR')").first
        crear_btn.wait_for(timeout=15_000)
        crear_btn.click()
        time.sleep(2.0)
        pw._ss("qp0567_03_crear")

        act_input = page.locator(
            "tr.o_selected_row [name='activities_id'] input, [name='activities_id'] input"
        ).first
        act_input.wait_for(timeout=15_000)
        act_input.click()
        time.sleep(0.3)
        act_input.press_sequentially(ACT_CODE, delay=80)
        time.sleep(1.5)
        pw._ss("qp0567_04_autocomplete")

        opcion = page.locator(f"li:has-text('{ACT_CODE}')").first
        if opcion.count() == 0:
            opcion = page.locator(".o_m2o_dropdown_option").first
        opcion.wait_for(state="visible", timeout=8000)
        print(f"    Seleccionando: '{opcion.inner_text().strip()}'")
        opcion.click()
        time.sleep(1.0)

        # Llenar proyectado
        amt = page.locator(
            "tr.o_selected_row [name='amount'] input, tr.o_selected_row input[name='amount']"
        ).last
        if amt.count() > 0 and amt.is_visible(timeout=2000):
            amt.click(click_count=3)
            amt.fill(str(int(CANTIDAD)))
            time.sleep(0.3)

        pw._ss("qp0567_05_amount")

        # GUARDAR la actividad (escribe en SGS)
        save_btn = page.locator("button:has-text('GUARDAR'), button:has-text('Guardar')").first
        save_btn.wait_for(timeout=15_000)
        save_btn.click()
        try:
            page.wait_for_selector(
                "button:has-text('GUARDAR'), button:has-text('Guardar')",
                state="hidden", timeout=10000
            )
        except PWTimeout:
            time.sleep(3.0)
        pw._check_error_modal()
        pw._ss("qp0567_06_creada")
        print(f"    Actividad {ACT_CODE} creada OK")
        time.sleep(1.5)
    else:
        print(f"[3] Actividad {ACT_CODE} ya existe en la lista")

    # ── 4. Encontrar fila → action_show_details → modal ──────────────────────
    print(f"[4] Abriendo modal de {ACT_CODE}...")
    row = page.locator(f".o_data_row:has-text('{ACT_CODE}')").first
    row.wait_for(state="visible", timeout=30_000)
    row.scroll_into_view_if_needed()
    time.sleep(0.5)
    pw._ss("qp0567_07_fila")

    detail_btn = row.locator("button[name='action_show_details']").first
    detail_btn.wait_for(state="visible", timeout=10_000)
    detail_btn.click()
    page.wait_for_selector(".modal", timeout=20_000)
    time.sleep(0.8)
    pw._ss("qp0567_08_modal")
    print("    Modal abierto OK")

    # ── 5. AGREGAR LÍNEA ──────────────────────────────────────────────────────
    print("[5] AGREGAR LÍNEA...")
    add_btn = page.locator(
        ".modal .o_field_x2many_list_row_add a, "
        ".modal a:has-text('AGREGAR'), .modal a:has-text('Agregar')"
    ).first
    add_btn.wait_for(state="visible", timeout=10_000)
    add_btn.click()
    time.sleep(0.8)
    pw._ss("qp0567_09_newline")

    # ── 6. Llenar cantidad ────────────────────────────────────────────────────
    print(f"[6] Cantidad = {CANTIDAD}")
    qty = page.locator(
        ".modal tr.o_selected_row input[name='amount'], "
        ".modal tr.o_selected_row [name='amount'] input, "
        ".modal [name='amount'] input"
    ).last
    qty.wait_for(state="visible", timeout=10_000)
    qty.click(click_count=3)
    qty.fill(str(int(CANTIDAD)))
    time.sleep(0.3)

    # ── 7. Llenar fecha ───────────────────────────────────────────────────────
    print(f"[7] Fecha = {FECHA_FIJA_MO}")
    date_inp = page.locator(
        ".modal tr.o_selected_row input[name='date'], "
        ".modal tr.o_selected_row [name='date'] input, "
        ".modal [name='date'] input"
    ).last
    fecha_ok = False
    try:
        if date_inp.count() > 0 and date_inp.is_visible(timeout=3000):
            date_inp.click(click_count=3)
            date_inp.fill(FECHA_FIJA_MO)
            date_inp.press("Tab")
            time.sleep(0.5)
            fecha_ok = True
            print(f"    Campo 'date' llenado con '{FECHA_FIJA_MO}' OK")
        else:
            print("    AVISO: campo date no encontrado en la fila nueva")
    except PWTimeout:
        print("    AVISO: timeout en campo date")
    pw._ss("qp0567_10_fecha")

    # ── 8. Llenar partner VILLAMIL ────────────────────────────────────────────
    print(f"[8] Partner = {PARTNER} (cedula {CEDULA})")
    p_inp = page.locator(
        ".modal tr.o_selected_row [name='partner_id'] input, "
        ".modal tr.o_selected_row input[name='partner_id'], "
        ".modal [name='partner_id'] input"
    ).last
    try:
        p_inp.wait_for(state="visible", timeout=10_000)
        p_inp.click()
        p_inp.fill("")
        p_inp.press_sequentially(CEDULA, delay=80)
        time.sleep(1.5)
        if not pw._click_m2o_option(PARTNER.strip().split()[0], timeout_s=3.0):
            print("    AVISO: VILLAMIL no encontrado en dropdown — puede no estar en el equipo aún")
        else:
            p_inp.press("Tab")
            time.sleep(0.5)
            print("    Partner seleccionado OK")
    except PWTimeout:
        print("    AVISO: timeout en campo partner")
    pw._ss("qp0567_11_partner")

    # ── 9. Screenshot final y DESCARTAR ──────────────────────────────────────
    print("\n[9] Screenshot final (modal con todos los campos)...")
    time.sleep(0.5)
    pw._ss("qp0567_12_final")

    print("\nDescartando modal (sin guardar avance)...")
    try:
        page.locator(
            ".o_dialog .modal-footer button:has-text('DESCARTAR'), "
            ".modal-footer button:has-text('Descartar'), "
            ".modal-footer button:has-text('DESCARTAR')"
        ).first.click()
        time.sleep(0.8)
    except Exception:
        page.keyboard.press("Escape")
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"  RESULTADO: fecha_ok={fecha_ok}")
    print(f"  Fecha llenada: {FECHA_FIJA_MO}")
    print(f"  Actividad QP0567 creada en {PRO_CODE} (proyectado=1.0, advance=0)")
    print(f"  Ningún avance escrito.")
    print(f"  Screenshot clave: logs/qp0567_10_fecha.png")
    print(f"{'='*60}\n")

except Exception as ex:
    print(f"\nERROR: {ex}")
    import traceback; traceback.print_exc()

pw.close()
print("Browser cerrado.")
