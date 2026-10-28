# -*- coding: utf-8 -*-
"""Carga el MB51 del SAP de Enel en Gestión General de SGO7 (2026-10-23). Solo lectura del archivo.

    python -I cargar_sap_gestion.py                         → «MB51*.xls*» de la carpeta semanal más nueva (Descargas\\exportes-sistemas\\AAAA-MM-DD\\)
                                                              o, si no hay, el más nuevo de Descargas
    python -I cargar_sap_gestion.py <archivo o carpeta>     → ese archivo (o el MB51 más nuevo de esa carpeta)
    python -I cargar_sap_gestion.py <…> --simular           → lee, cuenta y muestra, sin enviar
    python -I cargar_sap_gestion.py <…> --sin-recalculo     → el servidor no recalcula los casos (después: gestion-recalcular-todo.py)

El MB51 («Lista de documentos de material») no se exporta desde los sistemas del operador: llega por correo del cliente. Se guarda en la carpeta de la semana como
MB51DO_ACTUAL.XLSX. Es el material a cargo de Dominion (centro CD00, almacén CDMT, lotes por zona): lo que Enel entregó, lo que se
descargó contra cada LCL (261/262/281) y lo recuperado (965/966). No trae factura, HES ni pago.

Qué NO se manda: la columna «Nombre del usuario» (cédulas) y los textos libres (solo los que siguen un patrón conocido: ENELGEST,
TR2026-…, n.º de entrega, <LCL>…flujofactur, «Orden de trabajo en flujo de facturación», AS…, LI-…; lo demás va como «(texto libre)»).
Va a POST {api_url}/carga (registro del archivo: nombre, tamaño, sha256, fecha, filas, duplicados, fechas raras; corte = última fecha
de contabilización del consumo 261) y POST {api_url}/sap en lotes de 500 con X-Agente-Token de config_gestion.json (el token nunca se
imprime). Reenviar el mismo archivo no duplica (upsert por documento + posición). El archivo de Descargas es un dato no confiable: se lee
con openpyxl en modo solo lectura, sin macros ni fórmulas.
"""
import argparse
import getpass
import glob
import hashlib
import json
import os
import re
import socket
import sys
from collections import Counter
from datetime import date, datetime, timedelta

import openpyxl
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")
SEMANAL = os.path.join(DESCARGAS, "exportes-sistemas")
# Encabezado del MB51 (31 columnas, en este orden; la 25 es «Nombre del usuario» y NO se lee)
ENCABEZADO = ("Documento material", "Clase de movimiento", "Posición doc.mat.", "Material", "Descripción del material", "Centro", "Almacén", "Lote",
              "Centro receptor", "Almacén receptor", "Lote receptor", "Ctd.en UM entrada", "Un.medida de entrada", "Importe ML", "Moneda",
              "Fecha contabiliz.", "Fecha de entrada", "Hora de entrada", "Texto cab.documento", "Texto", "Texto de clase de movimiento",
              "Indicador Debe/Haber", "Cuenta de mayor", "Orden", "Nombre del usuario", "Orden", "Pedido", "Posición", "Referencia", "Grafo",
              "Carta Entrega Trabajos")
COL = {n: i for i, n in reversed(list(enumerate(ENCABEZADO)))}   # nombre → primera posición
I_ORDEN_2 = 25
I_USUARIO = 24
RX_OK = re.compile(r"^(ENELGEST|[A-Z]{1,4}20\d{2}-\d{1,6}|\d{4,12}|(63\d{8,9}|DF20\d{10})[A-Za-z]{0,6}flujofactur\w{0,6}|AS[\w\-]{0,20}|LI-[\w\-]{0,20})$")
RX_FLUJO = re.compile(r"^(63\d{8,9}|DF20\d{10})?\s*Orden de trabajo en flujo de facturaci\w{0,3}$", re.I)


def texto(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def seguro(t: str, posicion: bool = False) -> str:
    """El texto solo si sigue un patrón conocido (sin nombres ni cédulas); si no, «(texto libre)»."""
    s = re.sub(r"\s+", " ", t or "").strip()
    if not s:
        return ""
    if RX_OK.match(s) or (posicion and RX_FLUJO.match(s)):
        return s[:60]
    return "(texto libre)"


def num(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def fecha(v) -> str:
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = texto(v)
    m = re.match(r"(\d{2})[./](\d{2})[./](\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return s[:10] if re.match(r"\d{4}-\d{2}-\d{2}", s) else ""


def huella(ruta: str) -> dict:
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    st = os.stat(ruta)
    return {"_ruta": ruta, "archivo": os.path.basename(ruta)[:300], "sha256": h.hexdigest(), "tamano": st.st_size,       # _ruta = lo que se sube
            "modificado": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}

def subir_archivo(base, cab, cid, meta):
    """Sube al servidor el archivo de la carga (queda para «Descargar» en Gestión General y la descarga queda en Registros). El servidor
    comprueba que su sha256 es el de la carga; si ya lo tenía, no lo duplica. Un fallo aquí no detiene la carga: se avisa y sigue."""
    if not cid:
        return
    ruta, crudo = meta.get("_ruta"), meta.get("_crudo")
    nombre = meta.get("_nombre") or os.path.basename(ruta or meta.get("archivo") or "archivo")
    try:
        if crudo is None:
            with open(ruta, "rb") as fh:
                crudo = fh.read()
    except (OSError, TypeError) as e:
        print(f"   AVISO: no se pudo leer {nombre} para subirlo: {e}")
        return
    try:
        r = requests.post(f"{base}/carga/{cid}/archivo", headers=cab, timeout=900, data={"sha256": meta["sha256"]},
                          files={"archivo": (nombre, crudo, "application/octet-stream")})
    except requests.RequestException as e:
        print(f"   AVISO: el archivo {nombre} no se subió ({type(e).__name__}); la carga sigue sin él")
        return
    if r.status_code == 404:
        print("   AVISO: el servidor aún no recibe archivos de las cargas (anterior a 2026-10-25)")
        return
    if r.status_code >= 400:
        print(f"   AVISO: el archivo {nombre} no se subió (HTTP {r.status_code}: {r.text[:160]}); la carga sigue sin él")
        return
    print(f"   archivo {nombre} ({len(crudo):,} bytes) " + ("ya estaba en el servidor" if r.json().get("ya_estaba") else "subido al servidor")
          + ": queda para «Descargar»")


def archivo_por_defecto(arg) -> str:
    if arg and os.path.isfile(arg):
        return arg
    carpetas = [arg] if arg and os.path.isdir(arg) else sorted((d for d in glob.glob(os.path.join(SEMANAL, "20??-??-??")) if os.path.isdir(d)), reverse=True) + [DESCARGAS]
    for d in carpetas:
        xs = sorted(glob.glob(os.path.join(d, "MB51*.xls*")), key=os.path.getmtime)
        if xs:
            return xs[-1]
    sys.exit("No encontré un «MB51*.xlsx» (el MB51 llega por correo: guardarlo en la carpeta de la semana como MB51DO_ACTUAL.XLSX)")


def leer(ruta: str):
    wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    filas = ws.iter_rows(values_only=True)
    enc = tuple(texto(x) for x in next(filas))
    if enc[:len(ENCABEZADO)] != ENCABEZADO:
        wb.close()
        sys.exit(f"El MB51 cambió de forma: se esperaban {len(ENCABEZADO)} columnas conocidas y llegaron {list(enc)[:40]}")
    out, vistos, dup = [], set(), 0
    for r in filas:
        if not r or all(v is None for v in r):
            continue
        g = lambda n: r[COL[n]] if COL[n] < len(r) else None   # noqa: E731
        doc, pos = texto(g("Documento material")), texto(g("Posición doc.mat."))
        if not doc or not re.fullmatch(r"\d{1,8}", pos or ""):
            continue
        k = (doc, int(pos))
        dup += 1 if k in vistos else 0
        vistos.add(k)
        cab, txt = texto(g("Texto cab.documento")), texto(g("Texto"))
        out.append(dict(doc=doc[:20], pos=int(pos), clase=texto(g("Clase de movimiento"))[:4], material=texto(g("Material"))[:20],
                        descripcion=texto(g("Descripción del material"))[:120], cantidad=num(g("Ctd.en UM entrada")), um=texto(g("Un.medida de entrada"))[:10],
                        importe=num(g("Importe ML")), f_contab=fecha(g("Fecha contabiliz.")), lote=texto(g("Lote"))[:20], centro=texto(g("Centro"))[:10],
                        almacen=texto(g("Almacén"))[:10], centro_rec=texto(g("Centro receptor"))[:10], almacen_rec=texto(g("Almacén receptor"))[:10],
                        lote_rec=texto(g("Lote receptor"))[:20], carta=texto(g("Carta Entrega Trabajos"))[:60], orden=texto(g("Orden"))[:20],
                        orden_2=texto(r[I_ORDEN_2] if I_ORDEN_2 < len(r) else None)[:20], pedido=texto(g("Pedido"))[:20], referencia=texto(g("Referencia"))[:20],
                        cuenta=texto(g("Cuenta de mayor"))[:20], texto_cab=seguro(cab), texto=seguro(txt, posicion=True),
                        texto_clase=texto(g("Texto de clase de movimiento"))[:40], dh=texto(g("Indicador Debe/Haber"))[:1]))
        # la columna «Nombre del usuario» (r[I_USUARIO]) no se lee ni se guarda
    wb.close()
    return out, dup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ruta", nargs="?", default=None, help="el MB51 (.xlsx) o la carpeta donde está")
    ap.add_argument("--lote", type=int, default=500)
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--sin-recalculo", action="store_true")
    ap.add_argument("--config", default=os.path.join(DIR, "config_gestion.json"))
    a = ap.parse_args()
    ruta = archivo_por_defecto(a.ruta)
    print(f"MB51: {ruta}")
    filas, dup = leer(ruta)
    cons = [f for f in filas if f["clase"] in ("261", "262", "281")]
    corte = max((f["f_contab"] for f in filas if f["clase"] == "261" and f["f_contab"]), default="")
    tope = (date.today() + timedelta(days=2)).isoformat()
    fuera = sum(1 for f in filas if f["f_contab"] and (f["f_contab"] < "2020-01-01" or f["f_contab"] > tope))
    con_lcl = sum(1 for f in cons if re.fullmatch(r"63\d{8,9}|DF20\d{10}", f["carta"].upper()))
    clases = Counter(f["clase"] for f in filas)
    libres = sum(1 for f in filas if "(texto libre)" in (f["texto_cab"], f["texto"]))
    print(f"filas {len(filas)} · documentos {len({f['doc'] for f in filas})} · códigos {len({f['material'] for f in filas})} · repetidas (documento, posición) {dup}"
          f" · fechas fuera de rango {fuera} · textos libres que no se mandan {libres}")
    print(f"consumo (261/262/281) {len(cons)} filas · con LCL o DF {con_lcl} ({100 * con_lcl / max(1, len(cons)):.1f} %) · valor "
          f"${-sum(f['importe'] for f in cons) / 1e6:,.1f} M · consumo hasta el {corte}")
    print("clases:", dict(clases.most_common()))
    if a.simular:
        return
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    base = cfg["api_url"].rstrip("/")
    cab = {"X-Agente-Token": cfg["token"]}
    meta = huella(ruta)
    r = requests.post(base + "/carga", headers=cab, timeout=120, json=dict(
        accion="abrir", sistema="sap", parte="mb51", filas_archivo=len(filas), duplicados=dup, fuera_rango=fuera, corte=(f"{corte} 00:00:00" if corte else ""),
        usuario=getpass.getuser()[:100], equipo=socket.gethostname()[:100],
        detalle={"clases": dict(clases.most_common()), "consumo": len(cons), "consumo_con_lcl": con_lcl, "textos_libres": libres,
                 "nota": "MB51 de Enel: solo material (sin factura, HES ni pago); sin la columna de usuarios"}, **{k: v for k, v in meta.items() if not k.startswith("_")}))
    if r.status_code >= 400:
        sys.exit(f"carga sap/mb51: HTTP {r.status_code} {r.text[:200]}")
    cid = r.json().get("carga_id")
    print(f"   carga sap/mb51: {meta['archivo']} · sha256 {meta['sha256'][:12]}… · {len(filas)} filas → carga_id {cid}"
          + (" (mismo archivo que una carga anterior)" if r.json().get("repetida") else ""))
    subir_archivo(base, cab, cid, meta)
    tot = Counter()
    n_lotes = (len(filas) + a.lote - 1) // a.lote
    fin = {}
    for i in range(0, len(filas), a.lote):
        ult = i + a.lote >= len(filas)
        rr = requests.post(base + "/sap", headers=cab, timeout=900, json={"archivo": meta["archivo"][:200], "filas": filas[i:i + a.lote], "carga_id": cid,
                                                                          "fin": ult, "recalcular": not a.sin_recalculo})
        if rr.status_code >= 400:
            try:
                det = rr.json().get("detail")
            except Exception:
                det = rr.text[:200]
            sys.exit(f"sap: HTTP {rr.status_code} {det}")
        d = rr.json()
        for k in ("nuevas", "actualizadas", "ya_estaban", "invalidas"):
            tot[k] += d.get(k) or 0
        n = i // a.lote + 1
        if n % 10 == 0 or ult:
            print(f"  lote {n}/{n_lotes}: nuevas {tot['nuevas']} · actualizadas {tot['actualizadas']} · ya estaban {tot['ya_estaban']} · inválidas {tot['invalidas']}")
        if ult:
            fin = d
    ctl = fin.get("controles") or {}
    print(f"TOTAL SAP: nuevas {tot['nuevas']} · actualizadas {tot['actualizadas']} · ya estaban {tot['ya_estaban']} · inválidas {tot['invalidas']}"
          f" · filas en SGO7 {fin.get('filas_tabla')} · SAP al {fin.get('corte')}")
    print(f"precio de Enel: {fin.get('precios')}")
    rojo = set(fin.get("por_debajo") or [])
    for k, t in (("pct_lcl_en_scm", "LCL de SAP que están en SCM"), ("pct_pares_iguales", "pares (LCL, código) SAP = SCM"), ("pct_rec_iguales", "recuperado SAP = SCM")):
        print(f"   {'!! ROJO ' if k in rojo else ''}{t}: {ctl.get(k)} %")
    print(f"casos con SAP: {fin.get('casos_con_sap')}" + (" · el servidor los recalcula en segundo plano" if fin.get("recalculo_en_fondo") else " · sin recálculo"))
    res = {k: int(v) for k, v in tot.items()} | {"corte": fin.get("corte"), "precios": fin.get("precios"), "controles": ctl}
    rc = requests.post(base + "/carga", headers=cab, timeout=300, json=dict(accion="cerrar", carga_id=cid, filas_enviadas=len(filas), resultado=res))
    if rc.status_code < 400:
        c = [x for x in rc.json().get("controles") or [] if x["k"].startswith("sap_")]
        if c:
            print("   controles: " + " · ".join(f"{x['titulo']}: {x['valor']} ({x['estado']})" for x in c))
    else:
        print(f"AVISO: no se pudo cerrar la carga {cid}: HTTP {rc.status_code}")


if __name__ == "__main__":
    main()
