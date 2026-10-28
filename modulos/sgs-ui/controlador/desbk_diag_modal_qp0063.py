"""
Diagnóstico del modal "Avance del proyecto" para QP0063.
Abre el modal, inspecciona el DOM y devuelve los selectores disponibles.
"""
import sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from browser_client import PlaywrightClient

PID      = 51532
ACT_CODE = "QP0063"

pw = PlaywrightClient(headless=False, slow_mo=200)
pw.start()
page = pw._page

try:
    pw.login()

    # Navegar al proyecto
    pw._navegar_proyecto(PID, pro_code="PRO-054384")
    time.sleep(1)

    # Abrir lista de actividades
    page.locator("[name='action_button_activities']").first.click()
    time.sleep(2)
    for f in page.locator(".o_searchview .o_facet_remove").all():
        try: f.click(); time.sleep(0.3)
        except: pass
    time.sleep(1)

    # Abrir modal
    row = page.locator(f".o_data_row:has-text('{ACT_CODE}')").first
    row.wait_for(state="visible", timeout=15_000)
    row.scroll_into_view_if_needed()
    row.locator("button[name='action_show_details']").first.click()
    page.wait_for_selector(".modal, .o_dialog", timeout=15_000)
    time.sleep(1.5)

    print("\n=== MODAL ABIERTO ===")

    # Inspeccionar footer del modal
    footer_info = page.evaluate("""() => {
        const footers = document.querySelectorAll('.modal .modal-footer, .o_dialog .modal-footer, .o_dialog .o_dialog_footer');
        return Array.from(footers).map(f => ({
            class: f.className,
            buttons: Array.from(f.querySelectorAll('button')).map(b => ({
                text: b.textContent.trim(),
                class: b.className,
                type: b.type
            }))
        }));
    }""")
    print("\nFooter buttons:", footer_info)

    # Inspeccionar todos los [name='amount'] en el modal
    amount_info = page.evaluate("""() => {
        const dialog = document.querySelector('.modal, .o_dialog');
        if (!dialog) return 'NO DIALOG FOUND';
        const fields = dialog.querySelectorAll('[name]');
        return Array.from(fields).map(f => ({
            name: f.getAttribute('name'),
            tag: f.tagName,
            class: f.className.substring(0, 80),
            inputChildren: f.querySelectorAll('input').length,
            text: (f.tagName === 'INPUT') ? f.value : f.textContent.trim().substring(0, 30)
        })).filter(f => ['amount', 'advance_project_activities_ids', 'partner_id', 'date'].includes(f.name));
    }""")
    print("\nCampos relevantes en modal:", amount_info)

    # Inspeccionar estructura del dialog
    dialog_struct = page.evaluate("""() => {
        const d = document.querySelector('.o_dialog, .modal');
        if (!d) return 'NO DIALOG';
        return {
            class: d.className,
            hasFormView: !!d.querySelector('.o_form_view'),
            hasGroupFields: d.querySelectorAll('.o_field_widget').length,
            inputs: Array.from(d.querySelectorAll('input')).map(i => ({
                name: i.getAttribute('name'),
                id: i.id,
                class: i.className.substring(0, 60),
                value: i.value
            }))
        };
    }""")
    print("\nEstructura dialog:", dialog_struct)

    # Intentar click EDITAR si existe
    edit_buttons = page.locator(
        ".modal .modal-footer button, .o_dialog .modal-footer button"
    ).all()
    print(f"\nBotones en footer ({len(edit_buttons)}):")
    for btn in edit_buttons:
        try:
            print(f"  '{btn.inner_text()}' visible={btn.is_visible()}")
        except:
            pass

    # Intentar EDITAR con todos los posibles textos
    for txt in ['EDITAR', 'Editar', 'editar', 'Edit']:
        el = page.locator(f".modal .modal-footer button:has-text('{txt}'), .o_dialog .modal-footer button:has-text('{txt}')").first
        try:
            if el.count() > 0 and el.is_visible(timeout=500):
                print(f"\nEncontrado botón '{txt}' — clickeando...")
                el.click()
                time.sleep(1.5)
                break
        except:
            pass

    # Re-inspeccionar después del EDITAR
    print("\n=== DESPUÉS DE EDITAR (si fue clickeado) ===")
    amount_info2 = page.evaluate("""() => {
        const dialog = document.querySelector('.modal, .o_dialog');
        if (!dialog) return 'NO DIALOG';
        return Array.from(dialog.querySelectorAll('input')).map(i => ({
            name: i.getAttribute('name'),
            id: i.id,
            value: i.value,
            type: i.type,
            visible: i.offsetParent !== null
        }));
    }""")
    print("Inputs en modal post-EDITAR:", amount_info2)

    # Screenshot final
    page.screenshot(path="logs/diag_modal.png")
    print("\nScreenshot: logs/diag_modal.png")

    input("\nPresiona Enter para cerrar...")

finally:
    pw.close()
