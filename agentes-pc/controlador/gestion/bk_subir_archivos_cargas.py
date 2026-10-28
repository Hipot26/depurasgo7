# -*- coding: utf-8 -*-
"""subir_archivos_cargas.py — Gestión General (2026-10-25): sube al servidor el archivo de cada carga ya registrada (para el botón «Descargar»).

    python -I subir_archivos_cargas.py               → las cargas vigentes (la última de cada sistema y parte) que aún no tienen archivo
    python -I subir_archivos_cargas.py --todas       → también las cargas anteriores
    python -I subir_archivos_cargas.py --simular     → solo dice qué encontró y qué subiría

Desde 2026-10-25 cada cargador (cargar_*_gestion.py) sube su archivo al abrir la carga; este script rellena las cargas de antes. El servidor
(GET {api_url}/cargas) dice qué cargas hay, con su sha256 y su tamaño; aquí se buscan en Descargas, Descargas\\exportes-sistemas\\…,
Descargas\\SCM\\… y las carpetas «…exported-request…» (y dentro de los zip de PMS, cuyo CSV es el archivo de la carga) los archivos del mismo
tamaño, y solo se sube el que tiene EXACTAMENTE la misma huella sha256. Si ninguno coincide, no se sube nada y se dice. El servidor vuelve a
comprobar la huella y no duplica lo que ya tiene. Los archivos de Descargas son datos no confiables: solo se leen sus bytes para la huella y
se suben tal cual. El token vive en config_gestion.json.
"""
import argparse
import hashlib
import json
import os
import sys
import zipfile

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")
EXT = (".xls", ".xlsx", ".csv", ".zip", ".json")


def candidatos(raices, profundidad=4):
    """Los archivos de exporte (por extensión) en las carpetas dadas, hasta cierta profundidad."""
    vistos = set()
    for raiz in raices:
        if not os.path.isdir(raiz):
            continue
        base = raiz.rstrip("\\/").count(os.sep)
        for d, subdirs, archivos in os.walk(raiz):
            if d.count(os.sep) - base >= profundidad:
                subdirs[:] = []
            for a in archivos:
                r = os.path.join(d, a)
                if a.lower().endswith(EXT) and r not in vistos:
                    vistos.add(r)
                    yield r


def sha_de(ruta):
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--todas", action="store_true")
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--config", default=os.path.join(DIR, "config_gestion.json"))
    ap.add_argument("--carpeta", action="append", default=[], help="otra carpeta donde buscar (se puede repetir)")
    a = ap.parse_args()
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    base = cfg["api_url"].rstrip("/")
    cab = {"X-Agente-Token": cfg["token"]}
    r = requests.get(base + "/cargas", headers=cab, timeout=120)
    if r.status_code >= 400:
        sys.exit(f"el servidor no da la lista de cargas: HTTP {r.status_code} {r.text[:160]}")
    cargas = [c for c in r.json().get("cargas") or [] if (a.todas or c.get("ultima")) and not c.get("tiene_archivo")]
    ya = [c for c in r.json().get("cargas") or [] if (a.todas or c.get("ultima")) and c.get("tiene_archivo")]
    print(f"cargas {'todas' if a.todas else 'vigentes'} sin archivo en el servidor: {len(cargas)} · ya con archivo: {len(ya)}")
    if not cargas:
        return
    por_tam = {}
    for c in cargas:
        por_tam.setdefault(int(c.get("tamano") or 0), []).append(c)
    raices = a.carpeta + [os.path.join(DESCARGAS, "exportes-sistemas"), os.path.join(DESCARGAS, "SCM"), DESCARGAS]
    hallados = {}              # carga_id → (nombre, bytes o ruta, de dónde)
    for ruta in candidatos(raices):
        try:
            tam = os.path.getsize(ruta)
        except OSError:
            continue
        if tam in por_tam:
            s = sha_de(ruta)
            for c in por_tam[tam]:
                if c["sha256"] == s and c["id"] not in hallados:
                    hallados[c["id"]] = (os.path.basename(ruta), ruta, ruta)
        if ruta.lower().endswith(".zip"):           # PMS: el archivo de la carga es el CSV de adentro del zip
            try:
                with zipfile.ZipFile(ruta) as zf:
                    for zi in zf.infolist():
                        if zi.file_size in por_tam and zi.filename.lower().endswith((".csv", ".xls", ".xlsx")):
                            crudo = zf.read(zi)
                            s = hashlib.sha256(crudo).hexdigest()
                            for c in por_tam[zi.file_size]:
                                if c["sha256"] == s and c["id"] not in hallados:
                                    hallados[c["id"]] = (os.path.basename(zi.filename), crudo, f"{ruta}:{zi.filename}")
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue
    subidas, fallas = 0, []
    for c in sorted(cargas, key=lambda x: x["id"]):
        h = hallados.get(c["id"])
        etq = f"carga {c['id']} {c['sistema']}/{c['parte']} «{c['archivo']}»"
        if not h:
            print(f"   {etq}: NO HALLADO (ningún archivo con su huella {c['sha256'][:12]}… y {c.get('tamano', 0):,} bytes) — no se sube")
            fallas.append(c)
            continue
        nombre, fuente, donde = h
        if a.simular:
            print(f"   {etq}: subiría {donde}")
            continue
        crudo = fuente if isinstance(fuente, bytes) else open(fuente, "rb").read()
        rr = requests.post(f"{base}/carga/{c['id']}/archivo", headers=cab, timeout=900, data={"sha256": c["sha256"]},
                           files={"archivo": (nombre, crudo, "application/octet-stream")})
        if rr.status_code >= 400:
            print(f"   {etq}: el servidor no lo aceptó (HTTP {rr.status_code}: {rr.text[:160]})")
            fallas.append(c)
            continue
        subidas += 1
        print(f"   {etq}: {'ya estaba' if rr.json().get('ya_estaba') else 'subido'} ← {donde}")
    print(f"listo: {subidas} subidas · {len(fallas)} sin archivo" + (f" ({', '.join(str(c['id']) for c in fallas)})" if fallas else ""))


if __name__ == "__main__":
    main()
