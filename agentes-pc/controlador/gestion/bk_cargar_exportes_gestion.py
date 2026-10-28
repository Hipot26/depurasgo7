# -*- coding: utf-8 -*-
"""La carga semanal del hilo del dinero en Gestión General de SGO7 (2026-10-23), en un solo paso.

    python -I cargar_exportes_gestion.py              → junta los exportes de hoy y los sube (Sonny, PMS, SCM + deuda) y hace nacer los casos
    python -I cargar_exportes_gestion.py --simular    → solo dice qué movería y qué contaría cada carga, sin enviar
    python -I cargar_exportes_gestion.py --carpeta C:\\...\\exportes-sistemas\\2026-10-27   → usa esa carpeta tal cual

1. Crea Descargas\\exportes-sistemas\\AAAA-MM-DD\\ (hoy) y MUEVE ahí, desde Descargas, el más nuevo de cada exporte de los últimos
   3 días: «Historial de Planillas*.xlsx», «Lista de Planillas*.xlsx» (Sonny), «*exported-request*.zip» o la carpeta
   «*exported-request*» (PMS), «libretti_export*.xls», «prestazioni_export*.xls», «materiali_export*.xls» (SCM) y, si está,
   «*Informe deuda*.xlsx» y el MB51 de SAP «MB51*.xlsx» (llega por correo de Enel; opcional: si no está, lo dice y sigue). Si un exporte
   ya está en la carpeta (porque ya se había dejado ahí), no lo busca en Descargas.
   Desde 2026-10-25: también, opcionales, «Lista Cruzada de Nodos*.xlsx» (Contrato JA10185664), «Nodos por gestion almacen*.xlsx»,
   «Historial de Actividades*.xlsx» y «Lista de Estados*.xlsx» de Sonny: cargar_sonny_gestion.py los sube con los de planillas («Sonny por etapa»).
   Desde 2026-10-25: también, opcional, el exporte de LCL de SCM «lcl_export*.xls» (estado de cada LCL: CONCLUSA = concluida según SCM). Va con los
   3 de SCM (cargar_scm_gestion.py lo sube después de los libretos) o, si solo está él, con cargar_scm_gestion.py --solo-lcl.
   Desde 2026-10-25 la carga semanal usa solo exportes de los sistemas. Cada cargador sube además el archivo de su carga al
   servidor, para el botón «Descargar» de Gestión General.
2. Corre cargar_sonny_gestion.py, cargar_pms_gestion.py, cargar_scm_gestion.py, cargar_sap_gestion.py y nacer_casos_gestion.py con esa carpeta. Lo que
   falte se salta con aviso. Al final el servidor ya recalculó los casos tocados; la vuelta completa (SGS en vivo) la hace
   «Recalcular» en https://<host>/gestion.
Los exportes son datos no confiables: solo se mueven y los leen los scripts con pandas. El token vive en config_gestion.json.
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
import time
from datetime import date

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")
SEMANAL = os.path.join(DESCARGAS, "exportes-sistemas")
PATRONES = [("Sonny", "Historial de Planillas*.xls*", True), ("Sonny", "Lista de Planillas*.xls*", True),
            ("PMS", "*exported-request*", True), ("SCM", "libretti_export*.xls*", True), ("SCM", "prestazioni_export*.xls*", True),
            ("SCM", "materiali_export*.xls*", True), ("deuda", "*Informe deuda*.xls*", False),
            ("SCM", "lcl_export*.xls*", False),    # el exporte de LCL (estado de la LCL, validez, Importe, Trabajo = orden de SAP); opcional
            ("SAP", "MB51*.xls*", False),          # el MB51 llega por correo (llega por correo del cliente); opcional
            # Sonny por etapa (opcionales: sin ellos la fila «Sonny por etapa» sigue con lo que ya está cargado)
            ("Sonny", "Lista Cruzada de Nodos*.xls*", False), ("Sonny", "Nodos por gestion almacen*.xls*", False),
            ("Sonny", "Historial de Actividades*.xls*", False), ("Sonny", "Lista de Estados*.xls*", False)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carpeta", default=None)
    ap.add_argument("--dias", type=int, default=3, help="antigüedad máxima de un exporte en Descargas para tomarlo")
    ap.add_argument("--simular", action="store_true")
    a = ap.parse_args()
    carpeta = a.carpeta or os.path.join(SEMANAL, date.today().isoformat())
    print(f"carpeta de la semana: {carpeta}")
    if not a.simular:
        os.makedirs(carpeta, exist_ok=True)
    tiene = {}
    for sistema, patron, obligatorio in PATRONES:
        ya = glob.glob(os.path.join(carpeta, patron))
        if ya:
            tiene[patron] = True
            print(f"   {sistema}: ya está {os.path.basename(sorted(ya, key=os.path.getmtime)[-1])}")
            continue
        if a.carpeta:
            tiene[patron] = False
            print(f"   {sistema}: falta «{patron}» en la carpeta" + (" (obligatorio)" if obligatorio else " (opcional)"))
            continue
        xs = [x for x in glob.glob(os.path.join(DESCARGAS, patron)) if time.time() - os.path.getmtime(x) <= a.dias * 86400]
        if not xs:
            tiene[patron] = False
            print(f"   {sistema}: no hay «{patron}» de los últimos {a.dias} días en Descargas" + (" — se salta" if obligatorio else " (opcional)"))
            continue
        x = sorted(xs, key=os.path.getmtime)[-1]
        print(f"   {sistema}: {'movería' if a.simular else 'muevo'} {os.path.basename(x)}")
        if not a.simular:
            shutil.move(x, os.path.join(carpeta, os.path.basename(x)))
        tiene[patron] = True
    py = sys.executable
    pasos = []
    if tiene.get("Historial de Planillas*.xls*") and tiene.get("Lista de Planillas*.xls*"):
        pasos.append("cargar_sonny_gestion.py")          # sube también Lista Cruzada, gestión almacén, Actividades y Estados si están
    if tiene.get("*exported-request*"):
        pasos.append("cargar_pms_gestion.py")
    if all(tiene.get(p) for p in ("libretti_export*.xls*", "prestazioni_export*.xls*", "materiali_export*.xls*")):
        pasos.append("cargar_scm_gestion.py")              # sube también el lcl_export si está en la carpeta
    elif tiene.get("lcl_export*.xls*"):
        pasos.append(("cargar_scm_gestion.py", "--solo-lcl"))
    if tiene.get("MB51*.xls*"):                     # después de SCM (las órdenes «Trabajo» de SCM dan la confianza del cruce con SAP)
        pasos.append("cargar_sap_gestion.py")
    else:
        print("\n   SAP: no está el MB51 (llega por correo; guardarlo en la carpeta de la semana como MB51DO_ACTUAL.XLSX) — sigo sin él")
    for s in pasos:
        s, extra = (s if isinstance(s, tuple) else (s, ""))
        print(f"\n== {s} {extra}".rstrip())
        args = [py, "-I", os.path.join(DIR, s)]
        args += [carpeta] if not a.simular or os.path.isdir(carpeta) else []
        args += [extra] if extra else []
        args += ["--simular"] if a.simular else []
        r = subprocess.run(args, cwd=DIR)
        if r.returncode:
            sys.exit(f"{s} terminó con error ({r.returncode}); se detiene la carga")
    if pasos:
        print("\n== nacer_casos_gestion.py")
        subprocess.run([py, "-I", os.path.join(DIR, "nacer_casos_gestion.py")] + (["--simular"] if a.simular else []), cwd=DIR)
    else:
        print("\nNo hay exportes para cargar.")


if __name__ == "__main__":
    main()
