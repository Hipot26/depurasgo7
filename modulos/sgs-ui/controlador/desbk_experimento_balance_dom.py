"""
Experimento fix balance fresco — PRO-051568 — 7 materiales — RIOS ANAYA LEANDRO
Cada worker consulta el balance API fresco justo antes de liquidar.
"""
import sys, time, threading, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.join("..", "SGS + SONY", "sgs_api"))
from browser_client import PlaywrightClient
from sgs_readonly_client import SGSReadOnlyClient

PRO     = "PRO-051568"
PARTNER = "RIOS ANAYA LEANDRO"

ITEMS = [
    {"pm_id": 510861, "codigo": "DOM-038866",   "cantidad": 1.0},
    {"pm_id": 510863, "codigo": "DOM-039268",   "cantidad": 1.0},
    {"pm_id": 510864, "codigo": "DOM-028733-4", "cantidad": 4.0},
    {"pm_id": 510866, "codigo": "DOM-028954-8", "cantidad": 4.0},
    {"pm_id": 510867, "codigo": "DOM-028954-4", "cantidad": 6.0},  # devol parcial — DOM juez
    {"pm_id": 510868, "codigo": "DOM-037845",   "cantidad": 1.0},
    {"pm_id": 512537, "codigo": "DOM-057054",   "cantidad": 1.0},
]

print("=" * 60)
print(f"  FIX BALANCE DOM — {PRO} ({len(ITEMS)} materiales)")
print(f"  Partner: {PARTNER}")
print(f"  2 workers — slow_mo=100")
print("=" * 60)

pw_setup = PlaywrightClient(headless=True, slow_mo=100)
pw_setup.start()
pw_setup.login()
storage_state = pw_setup._page.context.storage_state()
pw_setup.close()
print("  Login OK\n")

# Cliente SGS solo lectura para consultas frescas de balance
_sgs = SGSReadOnlyClient("https://cofe.sgsdominion-global.com", "DOMINION_CO_002", "1051064568", "1051064568")
_sgs.autenticar()

def balance_fresco(pm_id: int) -> float:
    """Consulta el balance actual directamente desde SGS, sin caché."""
    rows = _sgs.search_read("project.material", [["id","=",pm_id]], ["balance"], limit=1)
    return rows[0]["balance"] if rows else 0.0

resultados = [None] * len(ITEMS)
lock = threading.Lock()

def worker(worker_id, items_idx):
    pw = PlaywrightClient(headless=True, slow_mo=100)
    pw.start(storage_state=storage_state)
    for idx in items_idx:
        item = ITEMS[idx]
        t0 = time.time()

        # Consulta fresca del balance justo antes de liquidar
        bal = balance_fresco(item["pm_id"])
        cantidad_real = min(item["cantidad"], bal)
        with lock:
            ajuste = f" ⚠ ajustado {item['cantidad']}→{cantidad_real}" if cantidad_real != item["cantidad"] else ""
            print(f"  [{time.strftime('%H:%M:%S')}] W{worker_id} {item['codigo']} "
                  f"pm#{item['pm_id']} qty={cantidad_real}{ajuste}...")

        if cantidad_real <= 0:
            with lock:
                print(f"    W{worker_id} → ✗ balance=0 — omitido")
            resultados[idx] = {"res": {"ok": False, "error": "balance=0"}, "t": 0}
            continue

        res = pw.liquidar_material(
            pm_id          = item["pm_id"],
            cantidad       = cantidad_real,
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
w0 = threading.Thread(target=worker, args=(0, [0, 2, 4, 6]))  # 4 items
w1 = threading.Thread(target=worker, args=(1, [1, 3, 5]))     # 3 items
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
print(f"  Promedio:       {prom:.1f}s/mat")
print(f"  Total paralelo: {total:.1f}s")
print(f"  Baseline ~17.2s: ~{17.2*len(ITEMS):.0f}s secuencial")
