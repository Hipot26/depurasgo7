"""
descubrir_campos_tray.py — Descubre los campos de project.tray en SGS.
Ejecutar UNA VEZ para identificar el nombre exacto del campo "Estado Entrega".

Uso:
    python descubrir_campos_tray.py
    python descubrir_campos_tray.py PRO-053609   # muestra bandejas reales del proyecto

Resultado: imprime los campos de project.tray y una muestra de datos reales.
Con esa info puedes actualizar _TRAY_DELIVERY_FIELD en:
  - modulos/julian/blueprint.py
  - modulos/verificador.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sgs_readonly_client import SGSReadOnlyClient

SGS_URL      = os.getenv("SGS_URL",      "https://cofe.sgsdominion-global.com")
SGS_LOGIN    = os.getenv("SGS_LOGIN",    "1051064568")
SGS_PASSWORD = os.getenv("SGS_PASSWORD", "1051064568")

c  = SGSReadOnlyClient(SGS_URL, "DOMINION_CO_002", SGS_LOGIN, SGS_PASSWORD)
c.autenticar()

print("\n=== CAMPOS DE project.tray ===\n")
fields = c.fields_get("project.tray", attributes=["string", "type", "selection"])

# Mostrar campos que podrían ser "Estado Entrega"
candidates = {}
for fname, finfo in sorted(fields.items()):
    label = str(finfo.get("string") or "").lower()
    ftype = finfo.get("type", "")
    if any(k in label for k in ("entrega", "delivery", "attend", "despacho", "estado", "state")):
        candidates[fname] = finfo
        sel = finfo.get("selection") or []
        print(f"  {fname!r:35} | {ftype:10} | {finfo.get('string')!r}")
        for v, l in sel:
            print(f"      valor={v!r:25} → etiqueta={l!r}")

if not candidates:
    print("  (ningún campo candidato encontrado — listando todos los selection fields)")
    for fname, finfo in sorted(fields.items()):
        if finfo.get("type") == "selection":
            sel = finfo.get("selection") or []
            print(f"  {fname!r:35} | {finfo.get('string')!r}")
            for v, l in sel:
                print(f"      valor={v!r:25} → etiqueta={l!r}")

# Si se pasa un PRO code, mostrar bandejas reales con todos sus campos
if len(sys.argv) > 1:
    pro_code = sys.argv[1]
    print(f"\n=== BANDEJAS DE {pro_code} (muestra de 3) ===\n")
    pro = c.buscar_por_codigo(pro_code)
    if not pro:
        print(f"Proyecto {pro_code} no encontrado")
    else:
        pid = pro["id"]
        all_field_names = list(fields.keys())
        try:
            trays = c.search_read("project.tray",
                                  [["project_id", "=", pid]],
                                  all_field_names, limit=5)
        except Exception:
            # Algunos campos pueden fallar; probar con subset
            safe_fields = ["id", "name", "type", "state", "partner_id", "date"]
            # Agregar candidatos conocidos
            safe_fields += [f for f in candidates]
            trays = c.search_read("project.tray",
                                  [["project_id", "=", pid]],
                                  safe_fields, limit=5)
        for t in trays[:3]:
            print(f"\n  BAN: {t.get('name')}  type={t.get('type')}  state={t.get('state')!r}")
            for k, v in sorted(t.items()):
                if k in ("project_tray_material_ids",):
                    continue
                if v is not False and v is not None and v != []:
                    print(f"    {k:35} = {v!r}")

print("\n=== FIN ===")
print("Actualizar _TRAY_DELIVERY_FIELD en blueprint.py y verificador.py con el campo correcto.")
