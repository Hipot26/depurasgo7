"""
test_fecha_modal_mo.py
======================
Abre el modal MO y verifica que FECHA_FIJA_MO="30/04/2026" aparece en el campo
"Fecha de avance" al agregar una línea. NO guarda nada.

Usa PRO-052077 / act_id=137193 / QP0567 que ya existe en SGS.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from browser_client import PlaywrightClient, FECHA_FIJA_MO, BrowserActionError
from playwright.sync_api import TimeoutError as PWTimeout

PID     = 49332
ACT_ID  = 137193
ACT_CODE = "QP0567"
PRO_CODE = "PRO-052077"

print(f"\n{'='*60}")
print(f"  TEST MODAL MO — Verificar FECHA_FIJA_MO en campo 'date'")
print(f"  Fecha esperada: {FECHA_FIJA_MO}")
print(f"{'='*60}\n")

pw = PlaywrightClient(headless=False, slow_mo=150)
pw.start()
pw.login()
print("Login OK\n")

page = pw._page

# 1. Navegar al proyecto
print(f"[1] Navegando a PRO-052077 pid={PID}...")
pw._navegar_proyecto(PID, pro_code=PRO_CODE)
pw._ss("test_01_proy")
print("    OK")

# 2. Abrir lista de actividades
print("[2] Abriendo lista de actividades...")
page.locator("[name='action_button_activities']").first.click()
time.sleep(2.0)
for f in page.locator(".o_searchview .o_facet_remove").all():
    try: f.click(); time.sleep(0.3)
    except: pass
time.sleep(1.0)
pw._ss("test_02_lista")
print("    OK")

# 3. Buscar la fila QP0567 → action_show_details → modal
print(f"[3] Buscando fila {ACT_CODE}...")
row = page.locator(f".o_data_row:has-text('{ACT_CODE}')").first
row.wait_for(state="visible", timeout=30_000)
row.scroll_into_view_if_needed()
time.sleep(0.5)
pw._ss("test_03_fila")

detail_btn = row.locator("button[name='action_show_details']").first
detail_btn.wait_for(state="visible", timeout=10_000)
detail_btn.click()
page.wait_for_selector(".modal", timeout=20_000)
time.sleep(0.8)
pw._ss("test_04_modal")
print("    Modal abierto OK")

# 4. AGREGAR LÍNEA en el modal
print("[4] Clic AGREGAR LÍNEA...")
add_sel = (
    ".modal .o_field_x2many_list_row_add a, "
    ".modal a:has-text('AGREGAR'), "
    ".modal a:has-text('Agregar')"
)
page.locator(add_sel).first.wait_for(state="visible", timeout=10_000)
page.locator(add_sel).first.click()
time.sleep(0.8)
pw._ss("test_05_newline")
print("    OK")

# 5. Llenar cantidad
print("[5] Llenando cantidad = 1...")
qty = page.locator(
    ".modal tr.o_selected_row input[name='amount'], "
    ".modal tr.o_selected_row [name='amount'] input"
).last
qty.wait_for(state="visible", timeout=10_000)
qty.click(click_count=3)
qty.fill("1")
time.sleep(0.3)
pw._ss("test_06_qty")

# 6. Llenar fecha (ESTO ES LO QUE QUEREMOS VERIFICAR)
print(f"[6] Llenando fecha → {FECHA_FIJA_MO}")
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
        print(f"    Campo date visible y llenado con '{FECHA_FIJA_MO}' ✓")
    else:
        print("    AVISO: campo date no visible (puede estar oculto por el layout)")
except PWTimeout:
    print("    AVISO: timeout buscando campo date")

pw._ss("test_07_fecha")

# 7. Screenshot final — muestra la fila con todos los campos
time.sleep(0.5)
pw._ss("test_08_final")

print(f"\n{'='*60}")
print(f"  RESULTADO: fecha_ok={fecha_ok}")
print(f"  Fecha llenada: {FECHA_FIJA_MO}")
print(f"  Screenshots en logs/test_0*.png")
print(f"{'='*60}\n")
print("Cerrando sin guardar (Escape)...")
try:
    page.keyboard.press("Escape")
    time.sleep(0.5)
except: pass

pw.close()
print("Listo. Revisa logs/test_07_fecha.png para ver el campo fecha.")
