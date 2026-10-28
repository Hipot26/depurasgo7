# -*- coding: utf-8 -*-
"""migrar_store.py — Adapta el pendientes.json que venía del PC al servidor SGO7 (idempotente).

En el PC las planillas vivían en Descargas, las sesiones del Balance en el nuevo/ local y los
enlaces a liquidador2 apuntaban a 127.0.0.1:5001. Nada de eso existe en el servidor:
  - todo registro: se quita `sid` (sesión del Balance local) y `liq2_url` pasa a ruta pública
    (/liquidador2/...) o se quita si era local;
  - registros ACTIVOS: si `excel_ruta` era una ruta de Windows, se guarda en `excel_ruta_pc`,
    se quita y queda la nota de regenerar aquí (planilla_lista vuelve a falta_info/listo_auto).
Marca `migrado_sgo7` en cada registro para no repetir. Uso: python3 migrar_store.py <ruta.json>
"""
import json
import re
import sys

ruta = sys.argv[1]
with open(ruta, encoding="utf-8") as f:
    d = json.load(f)

n = 0
for rid, r in d.items():
    if r.get("migrado_sgo7"):
        continue
    n += 1
    r["migrado_sgo7"] = "2026-10-14"
    r.pop("sid", None)
    u = r.get("liq2_url") or ""
    if u:
        m = re.search(r"/liquidador2/.*$", u)
        if m and ("127.0.0.1" in u or "localhost" in u):
            r["liq2_url"] = m.group(0)
        elif "127.0.0.1" in u or "localhost" in u:
            r.pop("liq2_url", None)
    activo = r.get("estado") not in ("completado", "descartado")
    ex = r.get("excel_ruta") or ""
    if activo and ex and (re.match(r"^[A-Za-z]:\\", ex) or "\\" in ex):
        r["excel_ruta_pc"] = ex
        r.pop("excel_ruta", None)
        r.setdefault("notas", []).append(
            "Migrado a SGO7 (14 oct 2026): la planilla generada en el PC quedó en Descargas del PC "
            "— genérala de nuevo aquí (⚡) o súbela (📤)")
        if r.get("estado") == "planilla_lista":
            r["estado"] = "falta_info"

with open(ruta, "w", encoding="utf-8") as f:
    json.dump(d, f, ensure_ascii=False, indent=1)
print(f"migrados {n} de {len(d)} registros")
