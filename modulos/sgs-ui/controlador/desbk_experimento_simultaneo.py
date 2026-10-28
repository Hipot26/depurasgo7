"""
Experimento simultáneo — 2 PROs en paralelo, 2 workers por PRO
PRO-051566 (6 mat) — RIOS ANAYA LEANDRO
PRO-051586 (22 mat) — ESCOBAR GOMEZ LUIS FELIPE
"""
import sys, time, threading
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from browser_client import PlaywrightClient

PROYECTOS = {
    "PRO-051566": {
        "partner": "RIOS ANAYA LEANDRO",
        "items": [
            {"pm_id": 510847, "codigo": "DOM-038866",   "cantidad": 1.0},
            {"pm_id": 510849, "codigo": "DOM-039268",   "cantidad": 1.0},
            {"pm_id": 510850, "codigo": "DOM-028733-4", "cantidad": 4.0},
            {"pm_id": 510852, "codigo": "DOM-028954-8", "cantidad": 4.0},
            {"pm_id": 510853, "codigo": "DOM-028954-4", "cantidad": 6.0},
            {"pm_id": 512521, "codigo": "DOM-057054",   "cantidad": 1.0},
        ],
    },
    "PRO-051586": {
        "partner": "ESCOBAR GOMEZ LUIS FELIPE",
        "items": [
            {"pm_id": 499782, "codigo": "DOM-040060",    "cantidad":  2.0},
            {"pm_id": 499783, "codigo": "DOM-028960-4",  "cantidad":  2.0},
            {"pm_id": 510869, "codigo": "DOM-038866",    "cantidad":  1.0},
            {"pm_id": 510871, "codigo": "DOM-028957",    "cantidad":  2.0},
            {"pm_id": 510872, "codigo": "DOM-028954-4",  "cantidad": 12.0},
            {"pm_id": 510873, "codigo": "DOM-028954-8",  "cantidad":  8.0},
            {"pm_id": 510874, "codigo": "DOM-038025",    "cantidad":  2.0},
            {"pm_id": 510875, "codigo": "DOM-028733-8",  "cantidad":  7.0},
            {"pm_id": 510876, "codigo": "DOM-039266",    "cantidad":  2.0},
            {"pm_id": 510877, "codigo": "DOM-052680-3",  "cantidad":  8.0},
            {"pm_id": 510878, "codigo": "DOM-037340",    "cantidad":  1.0},
            {"pm_id": 510879, "codigo": "DOM-037341",    "cantidad":  1.0},
            {"pm_id": 510880, "codigo": "DOM-037845",    "cantidad":  1.0},
            {"pm_id": 510881, "codigo": "DOM-028802-4",  "cantidad":  2.0},
            {"pm_id": 510883, "codigo": "DOM-037457",    "cantidad":  6.0},
            {"pm_id": 510884, "codigo": "DOM-028733-12", "cantidad":  1.0},
            {"pm_id": 510885, "codigo": "DOM-028840-5",  "cantidad":  8.0},
            {"pm_id": 510886, "codigo": "DOM-036639",    "cantidad":  2.0},
            {"pm_id": 510887, "codigo": "DOM-028846",    "cantidad":  8.0},
            {"pm_id": 510888, "codigo": "DOM-036954",    "cantidad": 50.0},
            {"pm_id": 512507, "codigo": "DOM-057054",    "cantidad":  1.0},
            {"pm_id": 512785, "codigo": "DOM-028733-4",  "cantidad":  2.0},
        ],
    },
}

print("=" * 65)
print("  EXPERIMENTO SIMULTÁNEO — 2 PROs × 2 workers")
total_mats = sum(len(p["items"]) for p in PROYECTOS.values())
print(f"  PRO-051566: 6 mat → RIOS ANAYA LEANDRO")
print(f"  PRO-051586: 22 mat → ESCOBAR GOMEZ LUIS FELIPE")
print(f"  Total: {total_mats} materiales")
print("=" * 65)

# Login único — storage_state compartido
pw_setup = PlaywrightClient(headless=True, slow_mo=100)
pw_setup.start()
pw_setup.login()
storage_state = pw_setup._page.context.storage_state()
pw_setup.close()
print("  Login OK — storage_state exportado\n")

lock = threading.Lock()

# resultados[pro_code][idx] = {res, t}
resultados = {pro: [None] * len(data["items"]) for pro, data in PROYECTOS.items()}

def worker(pro_code, worker_id, items_idx):
    data    = PROYECTOS[pro_code]
    items   = data["items"]
    partner = data["partner"]
    res_arr = resultados[pro_code]

    pw = PlaywrightClient(headless=True, slow_mo=100)
    pw.start(storage_state=storage_state)
    for idx in items_idx:
        item = items[idx]
        t0 = time.time()
        with lock:
            print(f"  [{time.strftime('%H:%M:%S')}] {pro_code} W{worker_id} "
                  f"{item['codigo']} pm#{item['pm_id']} qty={item['cantidad']}...")
        res = pw.liquidar_material(
            pm_id          = item["pm_id"],
            cantidad       = item["cantidad"],
            pro_code       = pro_code,
            partner_nombre = partner,
            dry_run        = False,
            screenshots    = False,
        )
        t = time.time() - t0
        estado = "✓ OK" if res.get("ok") else f"✗ {res.get('error','')[:60]}"
        with lock:
            print(f"    {pro_code} W{worker_id} → {estado}  ({t:.1f}s)")
        res_arr[idx] = {"res": res, "t": t}
    pw.close()

# Distribuir items por PRO
# PRO-051566: 6 items → W0=[0,2,4] W1=[1,3,5]
# PRO-051586: 22 items → W2=pares W3=impares
idx_566_w0 = [0, 2, 4]
idx_566_w1 = [1, 3, 5]
idx_586_w0 = list(range(0, 22, 2))   # 0,2,4,...,20  → 11 items
idx_586_w1 = list(range(1, 22, 2))   # 1,3,5,...,21  → 11 items

t_total = time.time()

w0 = threading.Thread(target=worker, args=("PRO-051566", 0, idx_566_w0))
w1 = threading.Thread(target=worker, args=("PRO-051566", 1, idx_566_w1))
w2 = threading.Thread(target=worker, args=("PRO-051586", 2, idx_586_w0))
w3 = threading.Thread(target=worker, args=("PRO-051586", 3, idx_586_w1))

# Arrancar: los 2 PROs simultáneos, stagger 3s entre workers del mismo PRO
w0.start()
w2.start()
time.sleep(3)
w1.start()
w3.start()

w0.join(); w1.join(); w2.join(); w3.join()
total = time.time() - t_total

print()
print("=" * 65)
print("  RESUMEN FINAL")
print("=" * 65)
for pro_code, res_arr in resultados.items():
    ok  = [r for r in res_arr if r and r["res"].get("ok")]
    err = [r for r in res_arr if r and not r["res"].get("ok")]
    tiempos = [r["t"] for r in res_arr if r]
    prom = sum(tiempos)/len(tiempos) if tiempos else 0
    n = len(PROYECTOS[pro_code]["items"])
    print(f"\n  {pro_code}:  OK={len(ok)}  Error={len(err)}  ({len(ok)}/{n})")
    if tiempos:
        print(f"    Promedio: {prom:.1f}s/mat")

print(f"\n  Tiempo total paralelo:      {total:.1f}s")
print(f"  Baseline secuencial ~17.2s: ~{17.2*total_mats:.0f}s")
print(f"  Ganancia:                   {17.2*total_mats/total:.1f}x")
