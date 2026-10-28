"""
LectorPlan — Lee el estado actual de un proyecto en SGS (SOLO LECTURA)
=======================================================================
Usa SGSReadOnlyClient. No escribe absolutamente nada.
Produce un plan de liquidación con los materiales pendientes.
"""

import sys
import os
import json
from datetime import datetime

from sgs_readonly_client import SGSReadOnlyClient

SGS_URL      = os.getenv("SGS_URL",      "https://cofe.sgsdominion-global.com")
SGS_LOGIN    = os.getenv("SGS_LOGIN",    "1051064568")
SGS_PASSWORD = os.getenv("SGS_PASSWORD", "1051064568")


def leer_plan(pro_code: str) -> dict:
    """
    Lee el estado completo de un proyecto y devuelve un plan:
    - materiales OK (ya liquidados con picking real)
    - materiales PENDIENTES (advance < despachado)
    - materiales REVISAR (sobre-liquidados o advance incorrecto)
    No toca nada.
    """
        c  = SGSReadOnlyClient(SGS_URL, "DOMINION_CO_002", SGS_LOGIN, SGS_PASSWORD)
    c.autenticar()

    pro = c.buscar_por_codigo(pro_code)
    if not pro:
        raise ValueError(f"Proyecto {pro_code} no encontrado")

    pid = pro["id"]

    # Leer materiales
    pm_all = c.search_read(
        "project.material",
        [["project_id", "=", pid]],
        ["id", "product_code", "despachado", "advance", "balance",
         "devuelto", "product_id"],
        limit=500,
    )

    # Leer todos los detalles del proyecto en una sola query
    all_dets = c.search_read(
        "project.material.details",
        [["project_material_id.project_id", "=", pid]],
        ["id", "project_material_id", "amount", "picking_id",
         "partner_id", "technical_stock", "state", "date"],
        limit=1000,
    )

    # Agrupar detalles por pm_id
    dets_by_pm: dict = {}
    for d in all_dets:
        pm_ref = d.get("project_material_id")
        pm_id  = pm_ref[0] if isinstance(pm_ref, list) else pm_ref
        dets_by_pm.setdefault(pm_id, []).append(d)

    # Leer bandejas para obtener partner por material
    bandejas = c.search_read(
        "project.tray",
        [["project_id", "=", pid], ["type", "=", "deliver"],
         ["state", "=", "approved"]],
        ["id", "name", "partner_id", "project_tray_material_ids"],
        limit=50,
    )

    # Construir partner_map por product_code
    partner_map = {}
    for ban in bandejas:
        mat_ids = ban.get("project_tray_material_ids") or []
        if not mat_ids:
            continue
        tms = c.search_read(
            "project.tray.material",
            [["id", "in", mat_ids]],
            ["id", "product_id", "partner_id", "deliver_quantity"],
            limit=200,
        )
        for tm in tms:
            prod_ref   = tm.get("product_id")
            partner_ref = tm.get("partner_id")
            if not prod_ref or not partner_ref:
                continue
            # Extraer código alternativo del nombre del producto
            prod_nombre = prod_ref[1] if isinstance(prod_ref, list) else ""
            # Formato: "[DOM-XXXXX/CODIGO] Descripcion"
            cod = None
            if "/" in prod_nombre and "]" in prod_nombre:
                cod = prod_nombre.split("/")[-1].split("]")[0].strip()
            if cod and cod not in partner_map:
                partner_map[cod] = {
                    "partner_id":     partner_ref[0] if isinstance(partner_ref, list) else None,
                    "partner_nombre": partner_ref[1] if isinstance(partner_ref, list) else "?",
                }

    # Clasificar materiales
    ok        = []
    pendientes = []
    revisar   = []

    for pm in pm_all:
        pm_id      = pm["id"]
        cod        = pm["product_code"]
        despachado = float(pm.get("despachado") or 0)
        advance    = float(pm.get("advance")    or 0)
        balance    = float(pm.get("balance")    or 0)

        if despachado == 0:
            continue  # sin movimiento — ignorar

        dets = dets_by_pm.get(pm_id, [])
        dets_con_picking = [d for d in dets
                             if d.get("picking_id") and float(d.get("amount") or 0) > 0]
        dets_sin_picking = [d for d in dets
                             if not d.get("picking_id") and float(d.get("amount") or 0) > 0]
        suma_ui  = sum(float(d.get("amount") or 0) for d in dets_con_picking)
        suma_api = sum(float(d.get("amount") or 0) for d in dets_sin_picking)

        partner = partner_map.get(cod, {})
        entrada = {
            "pm_id":           pm_id,
            "cod":             cod,
            "producto":        pm.get("product_id", [None, "?"])[1] if pm.get("product_id") else "?",
            "despachado":      despachado,
            "advance":         advance,
            "balance":         balance,
            "suma_ui":         suma_ui,
            "suma_api":        suma_api,
            "dets_ui":         dets_con_picking,
            "dets_api":        dets_sin_picking,
            "partner_id":      partner.get("partner_id"),
            "partner_nombre":  partner.get("partner_nombre", "?"),
        }

        if suma_api > 0:
            entrada["alerta"] = f"Tiene {len(dets_sin_picking)} detalle(s) API sin picking (suma={suma_api})"
            revisar.append(entrada)
        elif round(advance, 4) == round(despachado, 4) and len(dets_con_picking) > 0:
            ok.append(entrada)
        else:
            pendientes.append(entrada)

    return {
        "pro_code":   pro_code,
        "pro_id":     pid,
        "timestamp":  datetime.now().isoformat(),
        "ok":         ok,
        "pendientes": pendientes,
        "revisar":    revisar,
    }


def imprimir_plan(plan: dict):
    print(f"\n{'='*70}")
    print(f"PLAN DE LIQUIDACION — {plan['pro_code']}  ({plan['timestamp'][:16]})")
    print(f"{'='*70}")

    print(f"\n✅ YA LIQUIDADOS ({len(plan['ok'])}):")
    for m in plan["ok"]:
        print(f"  {m['cod']:<10} desp={m['despachado']} adv={m['advance']} | {m['partner_nombre']}")

    print(f"\n⏳ PENDIENTES ({len(plan['pendientes'])}):")
    for m in plan["pendientes"]:
        print(f"  {m['cod']:<10} desp={m['despachado']} adv={m['advance']} bal={m['balance']} | {m['partner_nombre']}")

    if plan["revisar"]:
        print(f"\n⚠️  REVISAR ({len(plan['revisar'])}):")
        for m in plan["revisar"]:
            print(f"  {m['cod']:<10} {m.get('alerta','')}")

    print()


if __name__ == "__main__":
    codigo = sys.argv[1] if len(sys.argv) > 1 else "PRO-052153"
    plan = leer_plan(codigo)
    imprimir_plan(plan)
    # Guardar JSON para que browser_client lo use
    out = os.path.join(os.path.dirname(__file__), "logs", f"plan_{codigo}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    print(f"Plan guardado en: {out}")
