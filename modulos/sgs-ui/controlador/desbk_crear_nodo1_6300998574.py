"""
CREAR NODO 1 — DF 6300998574  (parent: PRO-053607, id=50783)
=============================================================
Datos verificados contra SGS 2026-05-04:
  parent_id  = 50783  (PRO-053607, sin nodo — es el padre)
  contract   = Enel zona VII - 2026  (id=89)
  department = NZ7 -  Ubate TP       (id=138)
  technology = IPPC                   (id=37)
  formula    = ZVII ENEL - PxQ - 2026 (id=16)
  approver   = RUBIO MORAN MARTHA EUSEBIA (id=14136, solo en PRO-054560)
  ticket     = 6300998574
  date       = 04/16/2026

Nodos existentes: 4 (PRO-054560), 4AL6, 6, 5.  Nodo 1 NO existe.

USO:
  python crear_nodo1_6300998574.py            # dry_run (abre form, no guarda)
  python crear_nodo1_6300998574.py --real     # guarda en SGS (pide confirmacion)
"""
import sys, os, logging
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sgs_readonly_client import SGSReadOnlyClient
from browser_client import PlaywrightClient, BrowserActionError

# ── Datos del NODO 1 ──────────────────────────────────────────────────────────
DF_ID      = "6300998574"
PARENT_ID  = 50783            # PRO-053607 (padre sin nodo)
NODO_VAL   = "1"
NOMBRE     = f"{DF_ID} NODO {NODO_VAL}"
TICKET     = DF_ID
PDL        = "CDOPPI202677911;CDOPPI202678225"  # ambos PDLs del SIP

# Campos clonados de los hermanos (todos coinciden en el mismo contrato/dept/tech)
CONTRACT   = "Enel zona VII - 2026"
DEPARTMENT = "NZ7 -\xa0 Ubate TP"   # NBSP entre '-' y 'Ubate' — confirmado en SGS
TECHNOLOGY = "IPPC"                   # OJO: este proyecto usa IPPC, no PROYECTO
APPROVER   = "GUTIERREZ OTALORA WILKERSON"
DATE       = "04/16/2026"             # MM/DD/YYYY para el datepicker de Odoo

# ── Verificar en SGS que nodo 1 no existe ya (Regla R5) ──────────────────────
print("=" * 65)
print(f"  CREAR NODO 1 — DF {DF_ID}")
print("=" * 65)
print()

c = SGSReadOnlyClient(
    "https://cofe.sgsdominion-global.com",
    "DOMINION_CO_002", "1051064568", "1051064568",
)
c.autenticar()

hijos = c.search_read(
    "project.dominion",
    [["parent_id", "=", PARENT_ID]],
    ["id", "code", "name", "nodo"],
)
print(f"  Hijos actuales de PRO-053607 ({len(hijos)}):")
nodos_existentes = set()
for h in hijos:
    print(f"    {h['code']} — {h['name']} — nodo={h['nodo']}")
    nodos_existentes.add(str(h.get("nodo") or ""))

if NODO_VAL in nodos_existentes:
    print(f"\n  ✗ NODO {NODO_VAL} ya existe — abortando (Regla R5)")
    sys.exit(1)

print(f"\n  ✓ NODO {NODO_VAL} no existe — OK para crear")
print()
print(f"  Datos que se usarán:")
print(f"    nombre     = {NOMBRE}")
print(f"    parent_id  = {PARENT_ID}")
print(f"    nodo       = {NODO_VAL}")
print(f"    ticket     = {TICKET}")
print(f"    pdl        = {PDL}")
print(f"    contract   = {CONTRACT}")
print(f"    department = {repr(DEPARTMENT)}")
print(f"    technology = {TECHNOLOGY}")
print(f"    approver   = {APPROVER}")

# ── Dry run vs real ───────────────────────────────────────────────────────────
DRY_RUN = "--real" not in sys.argv

if DRY_RUN:
    print()
    print("  MODO: DRY RUN — el browser llenará el form pero NO guardará.")
    print("  Revisa que todo se vea correcto y luego corre:")
    print(f"    python {os.path.basename(__file__)} --real")
else:
    print()
    print("  MODO: REAL — SE GUARDARÁ en SGS.")
    confirm = input("  ¿Confirmar creación de NODO 1? (s/N): ").strip().lower()
    if confirm != "s":
        print("  Cancelado.")
        sys.exit(0)

print()

# ── Ejecutar ──────────────────────────────────────────────────────────────────
with PlaywrightClient(headless=False, slow_mo=300) as pw:
    pw.login()
    print("  [✓] Login OK")

    try:
        resultado = pw.crear_proyecto_hijo(
            parent_id=PARENT_ID,
            nombre=NOMBRE,
            nodo=NODO_VAL,
            ticket=TICKET,
            pdl=PDL,
            contract_id_name=CONTRACT,
            department_id_name=DEPARTMENT,
            technology_id_name=TECHNOLOGY,
            bandeja_approver_name=APPROVER,
            dry_run=DRY_RUN,
            screenshots=True,
        )
    except BrowserActionError as ex:
        print(f"\n  ✗ BrowserActionError: {ex}")
        print("  Screenshots en logs/crear_hijo_*.png")
        sys.exit(1)
    except Exception as ex:
        print(f"\n  ✗ Error inesperado: {ex}")
        import traceback; traceback.print_exc()
        sys.exit(1)

print()
print("=" * 65)
if resultado["status"] == "dry_run_ok":
    print("  DRY RUN completado — revisar el browser antes de cerrar.")
    print("  Screenshots:")
    for s in resultado.get("screenshots", []):
        print(f"    {s}")
    print()
    print(f"  Si todo se ve correcto:")
    print(f"    python {os.path.basename(__file__)} --real")
elif resultado["status"] == "ok":
    print(f"  ✓ NODO 1 CREADO — id={resultado['id']}")
    print("  Screenshots:")
    for s in resultado.get("screenshots", []):
        print(f"    {s}")
else:
    print(f"  Guardado ejecutado — status={resultado['status']}")
    print("  Verificar manualmente en SGS que el nodo apareció.")
    print("  ID detectado:", resultado.get("id"))
print("=" * 65)
