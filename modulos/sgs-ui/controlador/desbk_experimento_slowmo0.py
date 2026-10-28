"""
Experimento slow_mo=0 — PRO-051567 — 6 materiales — RIOS ANAYA LEANDRO
Comparar tiempos vs baseline slow_mo=100 (~17s/mat)
"""
import sys, time, threading
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from browser_client import PlaywrightClient

PRO     = "PRO-051567"
PARTNER = "RIOS ANAYA LEANDRO"

ITEMS = [
    {"pm_id": 510854, "codigo": "DOM-038866",   "cantidad": 1.0},
    {"pm_id": 510856, "codigo": "DOM-039268",   "cantidad": 1.0},
    {"pm_id": 510857, "codigo": "DOM-028733-4", "cantidad": 4.0},
    {"pm_id": 510859, "codigo": "DOM-028954-8", "cantidad": 4.0},
    {"pm_id": 510860, "codigo": "DOM-028954-4", "cantidad": 8.0},  # devol parcial, bal=8.0
    {"pm_id": 512529, "codigo": "DOM-057054",   "cantidad": 1.0},
]

print("=" * 60)
print(f"  EXPERIMENTO slow_mo=0 — {PRO} ({len(ITEMS)} materiales)")
print(f"  Partner: {PARTNER}")
print(f"  Baseline slow_mo=100: ~17.2s/mat")
print("=" * 60)

pw_setup = PlaywrightClient(headless=True, slow_mo=0)
pw_setup.start()
pw_setup.login()
storage_state = pw_setup._page.context.storage_state()
pw_setup.close()
print("  Login OK — storage_state exportado\n")

resultados = [None] * len(ITEMS)
lock = threading.Lock()

def worker(worker_id, items_idx):
    pw = PlaywrightClient(headless=True, slow_mo=0)
    pw.start(storage_state=storage_state)
    for idx in items_idx:
        item = ITEMS[idx]
        t0 = time.time()
        with lock:
            print(f"  [{time.strftime('%H:%M:%S')}] W{worker_id} {item['codigo']} "
                  f"pm#{item['pm_id']} qty={item['cantidad']}...")
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

t_total = time.time()
w0 = threading.Thread(target=worker, args=(0, [0, 2, 4]))
w1 = threading.Thread(target=worker, args=(1, [1, 3, 5]))
w0.start()
time.sleep(3)
w1.start()
w0.join()
w1.join()
total = time.time() - t_total

print()
print("=" * 60)
ok  = [r for r in resultados if r and r["res"].get("ok")]
err = [r for r in resultados if r and not r["res"].get("ok")]
tiempos = [r["t"] for r in resultados if r]
prom = sum(tiempos)/len(tiempos) if tiempos else 0
print(f"  OK: {len(ok)}  Error: {len(err)}")
print(f"  Tiempos: {' / '.join(f'{t:.1f}s' for t in tiempos)}")
print(f"  Promedio por material:  {prom:.1f}s  (baseline: 17.2s)")
print(f"  Tiempo total paralelo:  {total:.1f}s")
print(f"  Ganancia slow_mo:       {17.2/prom:.2f}x" if prom else "")
