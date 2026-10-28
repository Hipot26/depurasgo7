"""
Diagnóstico de estructura bandeja → material en SGS para PRO-052365.

Ejecutar:
  cd "c:/Users/duran/Documents/SGS + SONY/nuevo"
  python -B investigar_bandejas.py
"""
import os, sys, json

_BASE        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SGS_API_DIR = os.path.join(_BASE, "sgs_api")
if _SGS_API_DIR not in sys.path:
    sys.path.insert(0, _SGS_API_DIR)

from sgs_client import SGSClient, parsear_producto
from integracion import cfg_desde_archivo

cfg = cfg_desde_archivo()
if not cfg["sgs_db"]:
    cfg["sgs_db"] = SGSClient.detectar_db(cfg["sgs_url"])
    print(f"DB auto-detectada: {cfg['sgs_db']}")
c   = SGSClient(cfg["sgs_url"], cfg["sgs_db"], cfg["sgs_login"], cfg["sgs_password"])
c.autenticar()
print(f"Autenticado como uid={c.uid}\n")

# ── Buscar PRO-052365 ──────────────────────────────────────────────────────────
proyecto = c.buscar_por_codigo("PRO-052365")
if not proyecto:
    raise SystemExit("PRO-052365 no encontrado")

print(f"Proyecto encontrado: {proyecto['name']}  id={proyecto['id']}")
print(f"  code={proyecto.get('code')}  ODM={proyecto.get('order_number')}")
print()

# ── Nodos hijo ────────────────────────────────────────────────────────────────
nodos = c.obtener_nodos_hijo(proyecto["id"])
if not nodos:
    nodos = [proyecto]
print(f"Nodos encontrados: {len(nodos)}")
for n in nodos:
    print(f"  [{n['id']}] {n.get('name')} | nodo={n.get('nodo')}")
print()

# ── Bandejas por nodo ─────────────────────────────────────────────────────────
for nodo in nodos:
    nodo_str = nodo.get("nodo") or nodo.get("name")
    print(f"{'='*70}")
    print(f"NODO: {nodo_str}  (id={nodo['id']})")
    print(f"{'='*70}")

    bandejas = c.obtener_bandejas(nodo["id"])
    print(f"  Bandejas: {len(bandejas)}")

    for ban in bandejas:
        ban_name  = ban.get("name", "")
        ban_type  = ban.get("type", "")
        ban_state = ban.get("state", "")
        ban_date  = ban.get("date", "")
        mat_ids   = ban.get("project_tray_material_ids") or []
        print(f"\n  BAN: {ban_name}  tipo={ban_type}  estado={ban_state}  fecha={ban_date}")
        print(f"       {len(mat_ids)} materiales  ids={mat_ids[:5]}{'...' if len(mat_ids)>5 else ''}")

        materiales = c.obtener_materiales(mat_ids)
        for m in materiales:
            dom, alt, desc = parsear_producto(m.get("product_id"))
            qty  = m.get("deliver_quantity", 0)
            es_c = m.get("is_customer_material", False)
            print(f"       >> [{dom}/{alt}] {desc[:50]}  qty={qty}  es_cliente={es_c}")

    print()

    # ── Materiales de liquidación del nodo ────────────────────────────────────
    liq = c.obtener_materiales_liquidacion(nodo["id"])
    print(f"  Materiales liquidación (project.material): {len(liq)}")
    for m in liq:
        dom, alt, desc = parsear_producto(m.get("product_id"))
        cdc      = (m.get("customer_default_code") or "").strip()
        amount   = m.get("amount", 0)
        tray_val = m.get("tray", 0)
        desp     = m.get("despachado", 0)
        advance  = m.get("advance", 0)
        balance  = m.get("balance", 0)
        print(f"       [{dom}/{alt}] cdc={cdc!r:12} desc={desc[:35]:35}  "
              f"proy={amount} tray={tray_val} desp={desp} adv={advance} bal={balance}")

    print()

print("Diagnóstico completo.")
