"""
Experimento Opción D — 2 workers + intercepción de red
PRO-051581 — 5 materiales — RIOS ANAYA LEANDRO
"""
import sys, time, threading
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from browser_client import PlaywrightClient

PRO     = "PRO-051581"
PARTNER = "RIOS ANAYA LEANDRO"

ITEMS = [
    {"pm_id": 507985, "codigo": "274311", "cantidad": 5.0},
    {"pm_id": 507986, "codigo": "274312", "cantidad": 4.0},
    {"pm_id": 507991, "codigo": "274274", "cantidad": 2.0},
    {"pm_id": 507994, "codigo": "274287", "cantidad": 1.0},
    {"pm_id": 507995, "codigo": "251356", "cantidad": 8.0},
]

print("=" * 60)
print(f"  OPCIÓN D — 2 workers — PRO-051581 ({len(ITEMS)} materiales)")
print(f"  Partner: {PARTNER}")
print("=" * 60)

# Login fresco para obtener storage_state
pw_setup = PlaywrightClient(headless=True, slow_mo=100)
pw_setup.start()
pw_setup.login()
storage_state = pw_setup._page.context.storage_state()
pw_setup.close()
print(f"  Login OK — storage_state exportado\n")

resultados = [None] * len(ITEMS)
lock = threading.Lock()

def worker(worker_id, items_idx):
    pw = PlaywrightClient(headless=True, slow_mo=100)
    pw.start(storage_state=storage_state)
    for idx in items_idx:
        item = ITEMS[idx]
        t0 = time.time()
        with lock:
            print(f"  [{time.strftime('%H:%M:%S')}] W{worker_id} {item['codigo']} pm#{item['pm_id']} qty={item['cantidad']}...")
        res = pw.liquidar_material(
            pm_id          = item["pm_id"],
            cantidad       = item["cantidad"],
            pro_code       = PRO,
            partner_nombre = PARTNER,
            dry_run        = False,
            screenshots    = False,
        )
        t = time.time() - t0
        estado = "✓ OK" if res.get("ok") else f"✗ {res.get('error','')[:60]}"
        with lock:
            print(f"    W{worker_id} → {estado}  ({t:.1f}s)")
        resultados[idx] = {"res": res, "t": t}
    pw.close()

# Distribuir: W0 → items 0,2,4 / W1 → items 1,3
t_total = time.time()
w0 = threading.Thread(target=worker, args=(0, [0, 2, 4]))
w1 = threading.Thread(target=worker, args=(1, [1, 3]))
w0.start()
time.sleep(3)   # stagger mínimo
w1.start()
w0.join()
w1.join()
total = time.time() - t_total

print()
print("=" * 60)
ok  = [r for r in resultados if r and r["res"].get("ok")]
err = [r for r in resultados if r and not r["res"].get("ok")]
tiempos = [r["t"] for r in resultados if r]
print(f"  OK: {len(ok)}  Error: {len(err)}")
print(f"  Tiempos: {' / '.join(f'{t:.1f}s' for t in tiempos)}")
print(f"  Tiempo total paralelo: {total:.1f}s")
print(f"  Promedio por material: {sum(tiempos)/len(tiempos):.1f}s")
print(f"  Baseline 1 worker:     ~{17.2*len(ITEMS):.0f}s secuencial")
print(f"  Ganancia paralelismo:  {17.2*len(ITEMS)/total:.1f}x")
