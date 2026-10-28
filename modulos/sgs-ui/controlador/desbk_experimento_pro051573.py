"""
Experimento de timing — PRO-051573 — 13 materiales
Partner: RIOS ANAYA LEANDRO (desde project.tray, verificado)
Objetivo: medir tiempo real con nueva lógica sin networkidle
"""
import sys, time, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from browser_client import liquidar_materiales_parallel_v2

PARTNER = "RIOS ANAYA LEANDRO"
PRO     = "PRO-051573"

ITEMS = [
    {"tipo": "MAT", "idx": 0,  "pm_id": 508536, "codigo": "251364", "cantidad": 16.0, "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 1,  "pm_id": 508537, "codigo": "251363", "cantidad": 8.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 2,  "pm_id": 508539, "codigo": "274311", "cantidad": 7.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 3,  "pm_id": 508542, "codigo": "780180", "cantidad": 1.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 4,  "pm_id": 508543, "codigo": "860180", "cantidad": 1.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 5,  "pm_id": 508545, "codigo": "274274", "cantidad": 1.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 6,  "pm_id": 508546, "codigo": "274465", "cantidad": 1.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 7,  "pm_id": 508548, "codigo": "274287", "cantidad": 1.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 8,  "pm_id": 508549, "codigo": "251356", "cantidad": 8.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 9,  "pm_id": 508550, "codigo": "880030", "cantidad": 2.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 10, "pm_id": 508551, "codigo": "251367", "cantidad": 8.0,  "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 11, "pm_id": 508552, "codigo": "310410", "cantidad": 50.0, "pro_code": PRO, "partner_nombre": PARTNER},
    {"tipo": "MAT", "idx": 12, "pm_id": 517011, "codigo": "274300", "cantidad": 2.0,  "pro_code": PRO, "partner_nombre": PARTNER},
]

def progress_cb(orig_idx, worker_id, estado, codigo, error=None):
    ts = time.strftime("%H:%M:%S")
    if error:
        print(f"  [{ts}] W{worker_id} [{orig_idx}] {codigo} → {estado} ✗ {error[:80]}")
    else:
        print(f"  [{ts}] W{worker_id} [{orig_idx}] {codigo} → {estado}")

print("=" * 65)
print(f"  EXPERIMENTO PRO-051573 — {len(ITEMS)} materiales")
print(f"  Partner: {PARTNER}")
print(f"  Pool v2 — sin networkidle")
print("=" * 65)

# Login fresco para obtener storage_state (prueba fix de cids=1)
print("\n[SETUP] Login fresco para obtener storage_state...")
from browser_client import PlaywrightClient
t_login = time.time()
pw_login = PlaywrightClient(headless=True, slow_mo=100)
pw_login.start()
pw_login.login()
storage_state = pw_login._page.context.storage_state()
pw_login.close()
print(f"[SETUP] Login OK en {time.time()-t_login:.1f}s — storage_state exportado\n")

t0 = time.time()
resultados = liquidar_materiales_parallel_v2(
    ITEMS,
    storage_state=storage_state,
    progress_cb=progress_cb,
)
total = time.time() - t0

print()
print("=" * 65)
print(f"  RESULTADOS — {total:.1f}s total  ({total/len(ITEMS):.1f}s/material promedio)")
print("=" * 65)
ok  = [r for r in resultados if r.get("ok")]
err = [r for r in resultados if not r.get("ok")]
print(f"  OK:    {len(ok)}")
print(f"  Error: {len(err)}")
for r in err:
    print(f"    ✗ {r.get('codigo','?')} pm#{r.get('pm_id','?')}: {r.get('error','')[:100]}")
print()
print(f"  Tiempo por material: {total/len(ITEMS):.1f}s  (baseline anterior: ~70s)")
print(f"  Mejora estimada: {70/(total/len(ITEMS)):.1f}x")
