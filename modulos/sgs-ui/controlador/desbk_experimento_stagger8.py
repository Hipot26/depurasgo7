"""
Experimento stagger=8s — PRO-051565 — 22 materiales — 2 workers
Partners mixtos: RIOS ANAYA LEANDRO (21) + ESCOBAR GOMEZ LUIS FELIPE (1)
"""
import sys, time, threading, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join("..", "SGS + SONY", "sgs_api"))
from browser_client import PlaywrightClient
from sgs_readonly_client import SGSReadOnlyClient

PRO = "PRO-051565"

ITEMS = [
    {"pm_id": 499836, "codigo": "DOM-040060",    "cantidad":  2.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 499837, "codigo": "DOM-028960-4",  "cantidad":  2.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510706, "codigo": "DOM-038853",    "cantidad":  1.0, "partner": "ESCOBAR GOMEZ LUIS FELIPE"},
    {"pm_id": 510823, "codigo": "DOM-038866",    "cantidad":  1.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510825, "codigo": "DOM-028954-8",  "cantidad": 12.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510826, "codigo": "DOM-028954-4",  "cantidad": 12.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510827, "codigo": "DOM-038025",    "cantidad":  4.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510828, "codigo": "DOM-028733-8",  "cantidad":  5.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510829, "codigo": "DOM-028733-9",  "cantidad":  2.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510830, "codigo": "DOM-039266",    "cantidad":  1.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510831, "codigo": "DOM-052680-3",  "cantidad":  4.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510832, "codigo": "DOM-028846",    "cantidad":  5.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510835, "codigo": "DOM-037845",    "cantidad":  1.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510836, "codigo": "DOM-028802-4",  "cantidad":  1.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510837, "codigo": "DOM-037615",    "cantidad":  2.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510838, "codigo": "DOM-028906-3",  "cantidad":  3.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510839, "codigo": "DOM-028960-6",  "cantidad":  4.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510840, "codigo": "DOM-037408",    "cantidad":  6.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510843, "codigo": "DOM-028733-12", "cantidad":  1.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510844, "codigo": "DOM-028840-5",  "cantidad": 12.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510845, "codigo": "DOM-036639",    "cantidad":  3.0, "partner": "RIOS ANAYA LEANDRO"},
    {"pm_id": 510846, "codigo": "DOM-036954",    "cantidad": 55.0, "partner": "RIOS ANAYA LEANDRO"},
]

print("=" * 62)
print(f"  EXPERIMENTO stagger=8s — {PRO} ({len(ITEMS)} materiales)")
print(f"  RIOS×21 + ESCOBAR×1 — 2 workers")
print("=" * 62)

_sgs = SGSReadOnlyClient("https://cofe.sgsdominion-global.com", "DOMINION_CO_002", "1051064568", "1051064568")
_sgs.autenticar()

def balance_fresco(pm_id):
    rows = _sgs.search_read("project.material", [["id","=",pm_id]], ["balance"], limit=1)
    return rows[0]["balance"] if rows else 0.0

pw_setup = PlaywrightClient(headless=True, slow_mo=100)
pw_setup.start()
pw_setup.login()
storage_state = pw_setup._page.context.storage_state()
pw_setup.close()
print("  Login OK\n")

resultados = [None] * len(ITEMS)
lock = threading.Lock()

def worker(worker_id, items_idx):
    pw = PlaywrightClient(headless=True, slow_mo=100)
    pw.start(storage_state=storage_state)
    for idx in items_idx:
        item = ITEMS[idx]
        bal = balance_fresco(item["pm_id"])
        cantidad_real = min(item["cantidad"], bal)
        t0 = time.time()
        with lock:
            ajuste = f" ⚠→{cantidad_real}" if cantidad_real != item["cantidad"] else ""
            print(f"  [{time.strftime('%H:%M:%S')}] W{worker_id} {item['codigo']} "
                  f"pm#{item['pm_id']} qty={cantidad_real}{ajuste} [{item['partner'].split()[0]}]...")
        if cantidad_real <= 0:
            with lock:
                print(f"    W{worker_id} → ✗ balance=0 — omitido")
            resultados[idx] = {"res": {"ok": False, "error": "balance=0"}, "t": 0}
            continue
        res = pw.liquidar_material(
            pm_id          = item["pm_id"],
            cantidad       = cantidad_real,
            pro_code       = PRO,
            partner_nombre = item["partner"],
            dry_run        = False,
            screenshots    = False,
        )
        t = time.time() - t0
        estado = "✓ OK" if res.get("ok") else f"✗ {res.get('error','')[:55]}"
        with lock:
            print(f"    W{worker_id} → {estado}  ({t:.1f}s)")
        resultados[idx] = {"res": res, "t": t}
    pw.close()

# W0: items pares (11 items), W1: items impares (11 items)
# Stagger 8s — desfase natural entre saves
idx_w0 = list(range(0, 22, 2))
idx_w1 = list(range(1, 22, 2))

t_total = time.time()
w0 = threading.Thread(target=worker, args=(0, idx_w0))
w1 = threading.Thread(target=worker, args=(1, idx_w1))
w0.start()
time.sleep(8)   # stagger optimizado
w1.start()
w0.join()
w1.join()
total = time.time() - t_total

print()
print("=" * 62)
ok  = [r for r in resultados if r and r["res"].get("ok")]
err = [r for r in resultados if r and not r["res"].get("ok")]
tiempos = [r["t"] for r in resultados if r and r["t"] > 0]
prom = sum(tiempos)/len(tiempos) if tiempos else 0
print(f"  OK: {len(ok)}  Error: {len(err)}")
print(f"  Promedio por material: {prom:.1f}s")
print(f"  Tiempo total paralelo: {total:.1f}s")
print(f"  Baseline secuencial:   ~{17.2*len(ITEMS):.0f}s")
print(f"  Ganancia:              {17.2*len(ITEMS)/total:.2f}x")
