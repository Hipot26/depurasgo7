# -*- coding: utf-8 -*-
"""Carga los libretos de SCM (Enel) y el informe de deuda de facturación en Gestión General de SGO7 (2026-10-23).
Solo lectura de los exportes.

    python -I cargar_scm_gestion.py                       → la carpeta semanal más nueva (Descargas\\exportes-sistemas\\AAAA-MM-DD\\)
                                                            o, si no hay, Descargas\\SCM\\
    python -I cargar_scm_gestion.py <carpeta>              → esa carpeta
    python -I cargar_scm_gestion.py <carpeta> --simular    → arma, cuenta y muestra la MO en pesos, sin enviar
    python -I cargar_scm_gestion.py <carpeta> --sin-deuda  → no manda el informe de deuda aunque esté en la carpeta

En la carpeta (las 3 consultas de SCM, una por una; el informe de deuda es opcional):
  · «libretti_export*.xls»    (Informe LCL_LCA): un libreto por fila: LCL, número, descripción (PDL, DF y a veces el Id de la
                              planilla de Sonny), estado del libreto y del LCL, fecha de autocertificación.
  · «prestazioni_export*.xls» (Informe Prestaciones): MO por libreto en PUNTOS. Se pasa a pesos con cantidad × precio QP de
                              precios_mdo_mat.json (la lista del Balance Integrado; --precios para otra ruta). La huella de la
                              lista va al servidor.
  · «materiali_export*.xls»   (Informe Materiales): material posado y recuperado por libreto (sin precio: SCM no lo trae).
  · «*Informe deuda*.xlsx»    (facturación, hoja Data): solo LCL, QR, Id de planilla, PDL/ticket, libreto, MO, MAT, estado y
                              responsabilidad. Los nombres y cédulas del informe NO se leen ni se mandan.
Va a POST {api_url}/scm (lotes de ~200 libretos, LCL completos) y {api_url}/deuda con X-Agente-Token de config_gestion.json (el token
nunca se imprime). Reenviar los mismos archivos no duplica. Los .xls de Descargas son datos no confiables: se leen con pandas.
Desde 2026-10-23: cada libreto va con sus órdenes «Trabajo» del Informe Materiales (= «Orden» del MB51 de SAP), sin ceros a la izquierda.
Desde 2026-10-23: cada archivo queda registrado en el servidor como una carga (POST {api_url}/carga: nombre, tamaño, sha256, fecha de
modificación, filas, duplicados y fechas raras del archivo, usuario y equipo del PC) y sus filas llevan el carga_id. Cada libreto va con
sus prestaciones (código QP, unidad, cantidad, precio QP, pesos: las filas que suman su MO) y el material con su unidad.
Desde 2026-10-25: también el exporte de LCL «lcl_export*.xls» (Informe > «Informe LCL/LCA», que abre la pantalla «Informe Penalización y
Bonificaciones»: una fila por LCL × Trabajo, con el estado de la LCL —CONCLUSA = concluida según SCM—, envío, validez, Importe y Trabajo = orden de
SAP). Toma el más nuevo de la carpeta (o de su subcarpeta «scm») y, si no hay, el más nuevo de Descargas. Solo lee LCL, Estado, fechas, Importe,
Trabajo, Obra, Acumulativa y TAM: «Nombre técnico EDE», «Ejecutor» y la descripción NO se leen ni se mandan. Va a POST {api_url}/scm-lcl en lotes con
cada LCL completa; el último lote (fin) hace que el servidor recalcule lo que cambió. Es .xls viejo: pandas lo lee con xlrd.
    python -I cargar_scm_gestion.py --solo-lcl            → solo el exporte de LCL (el más nuevo de la carpeta semanal o de Descargas)
    python -I cargar_scm_gestion.py --lcl <archivo.xls>   → ese archivo de LCL (con los libretos de la carpeta, o solo él con --solo-lcl)
    python -I cargar_scm_gestion.py <carpeta> --sin-lcl   → los libretos sin el exporte de LCL
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
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")
SEMANAL = os.path.join(DESCARGAS, "exportes-sistemas")
PRECIOS = [os.path.join(os.path.expanduser("~"), "Documents", "sgo7-repo", "sony-sgs", "nuevo", "precios_mdo_mat.json"),
           os.path.join(DIR, "precios_mdo_mat.json")]
COLS_LIBRETTI = ["contrato", "lcl", "grupo_compras", "estado_lcl", "libreto", "descripcion", "estado_libreto", "responsable", "fecha_autocert"]


def texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def num_es(v) -> float:
    """«1.234,56» / «1,00» / «1234.5» → float."""
    s = texto(v)
    if not s:
        return 0.0
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def lcl_norm(v):
    d = re.sub(r"\D", "", texto(v)).lstrip("0")
    return d if d.startswith("63") and 10 <= len(d) <= 11 else None


def libreto_norm(v):
    s = re.sub(r"\s+", "", texto(v)).upper()
    if not s:
        return None
    return s.lstrip("0").zfill(3) if re.fullmatch(r"\d{1,4}", s) else s[:20]


def fecha_scm(v) -> str:
    """«05/06/2026 12.19.56» → «2026-06-05 12:19:56»."""
    s = texto(v)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2})[.:](\d{2})[.:](\d{2}))?", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)} {m.group(4) or '00'}:{m.group(5) or '00'}:{m.group(6) or '00'}"
    d = pd.to_datetime(s, errors="coerce", dayfirst=True)
    return "" if pd.isna(d) else d.strftime("%Y-%m-%d %H:%M:%S")


def carpeta_por_defecto() -> str:
    semanas = sorted(d for d in glob.glob(os.path.join(SEMANAL, "20??-??-??")) if os.path.isdir(d))
    for d in reversed(semanas):
        if glob.glob(os.path.join(d, "libretti_export*.xls*")):
            return d
    return os.path.join(DESCARGAS, "SCM")


def el_mas_nuevo(carpeta: str, patron: str, obligatorio: bool = True):
    xs = sorted(glob.glob(os.path.join(carpeta, patron)), key=os.path.getmtime)
    if not xs and obligatorio:
        sys.exit(f"No encontré «{patron}» en {carpeta}")
    return xs[-1] if xs else None


def corte_de(ruta: str) -> str:
    m = re.search(r"(20\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})", os.path.basename(ruta))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} {m.group(4)}:{m.group(5)}:{m.group(6)}"
    return datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%Y-%m-%d %H:%M:%S")


def huella(ruta: str) -> dict:
    """Sha256, tamaño y fecha de modificación (hora del PC = Colombia) de un archivo exportado."""
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    st = os.stat(ruta)
    return {"_ruta": ruta, "archivo": os.path.basename(ruta)[:300], "sha256": h.hexdigest(), "tamano": st.st_size,       # _ruta = lo que se sube
            "modificado": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


def fuera_de_rango(fechas) -> int:
    """Fechas antes de 2020 o más de 2 días en el futuro (las mismas reglas que el control del servidor)."""
    tope = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for f in fechas if f and (f < "2020-01-01" or f > tope))


def abrir_carga(base, cab, sistema, parte, meta, filas, duplicados=0, fuera_rango=0, corte="", detalle=None):
    """Registra la carga en el servidor y devuelve su carga_id (None si el servidor es anterior a 2026-10-23)."""
    r = requests.post(base + "/carga", headers=cab, timeout=120, json=dict(
        accion="abrir", sistema=sistema, parte=parte, filas_archivo=int(filas), duplicados=int(duplicados), fuera_rango=int(fuera_rango),
        corte=corte or "", usuario=getpass.getuser()[:100], equipo=socket.gethostname()[:100], detalle=detalle or {},
        **{k: v for k, v in meta.items() if not k.startswith("_")}))
    if r.status_code == 404:
        print("AVISO: el servidor no registra cargas (anterior a 2026-10-23); las filas van sin carga")
        return None
    if r.status_code >= 400:
        sys.exit(f"carga {sistema}/{parte}: HTTP {r.status_code} {r.text[:200]}")
    d = r.json()
    print(f"   carga {sistema}/{parte}: {meta['archivo']} · sha256 {meta['sha256'][:12]}… · {filas} filas → carga_id {d.get('carga_id')}"
          + (" (mismo archivo que una carga anterior)" if d.get("repetida") else ""))
    subir_archivo(base, cab, d.get("carga_id"), meta)
    return d.get("carga_id")


def cerrar_carga(base, cab, cid, enviadas, resultado):
    if not cid:
        return
    r = requests.post(base + "/carga", headers=cab, timeout=300, json=dict(accion="cerrar", carga_id=cid, filas_enviadas=int(enviadas), resultado=resultado))
    if r.status_code >= 400:
        print(f"AVISO: no se pudo cerrar la carga {cid}: HTTP {r.status_code}")
        return
    ctl = r.json().get("controles") or []
    if ctl:
        print("   controles: " + " · ".join(f"{c['titulo']}: {c['valor']} ({c['estado']})" for c in ctl))

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


def col(df, nombre):
    if nombre not in df.columns:
        sys.exit(f"El exporte no trae la columna «{nombre}» (columnas: {list(df.columns)})")
    return df[nombre]


def armar(carpeta: str, ruta_precios: str):
    f_lib = el_mas_nuevo(carpeta, "libretti_export*.xls*")
    f_pre = el_mas_nuevo(carpeta, "prestazioni_export*.xls*")
    f_mat = el_mas_nuevo(carpeta, "materiali_export*.xls*")
    with open(ruta_precios, "rb") as fh:
        crudo = fh.read()
    precios_sha = hashlib.sha256(crudo).hexdigest()
    precio = {k: float((v or {}).get("precio") or 0) for k, v in json.loads(crudo.decode("utf-8")).items()}
    metas = {"libretos": huella(f_lib), "prestaciones": huella(f_pre), "materiales": huella(f_mat)}
    li = pd.read_excel(f_lib, dtype=str, header=0)
    if len(li.columns) != len(COLS_LIBRETTI) or "LCL/LCA" not in li.columns or "Número Libreto" not in li.columns:
        sys.exit(f"El Informe LCL_LCA cambió de forma (columnas: {list(li.columns)})")
    li.columns = COLS_LIBRETTI
    pr = pd.read_excel(f_pre, dtype=str, header=0)
    ma = pd.read_excel(f_mat, dtype=str, header=0)
    print(f"SCM: {os.path.basename(f_lib)} {len(li)} libretos · {os.path.basename(f_pre)} {len(pr)} prestaciones · {os.path.basename(f_mat)} {len(ma)} materiales")
    print(f"precios QP: {ruta_precios} ({len(precio)} códigos, sha {precios_sha[:12]})")

    mo, pts, n_pre, sin_p, tks = defaultdict(float), defaultdict(float), Counter(), Counter(), defaultdict(set)
    prest = defaultdict(list)            # el detalle que suma la MO de cada libreto
    uni_pr = pr["Unidad de Medida"] if "Unidad de Medida" in pr.columns else [""] * len(pr)
    pu_pr = pr["Compensación Unitaria Puntos"] if "Compensación Unitaria Puntos" in pr.columns else [""] * len(pr)
    # llave (lcl, libreto, estado): SCM repite a veces el número con un libreto anulado y otro vivo (el mismo número con un libreto ANN y otro SAP)
    for lcl, lib, est, qp, cant, tot, tk, uni, pu in zip(col(pr, "Código LCL/LCA"), col(pr, "Código Libreto"), col(pr, "Estado Libreto"), col(pr, "Código Prestación"),
                                                         col(pr, "Cant."), col(pr, "Total presupuestado (M$)"), col(pr, "Ticket"), uni_pr, pu_pr):
        k = (lcl_norm(lcl), libreto_norm(lib), texto(est).upper())
        if not k[0] or not k[1]:
            continue
        c = num_es(cant)
        cod_qp = texto(qp).upper()
        p = precio.get(cod_qp)
        n_pre[k] += 1
        pesos = 0.0
        if p is None:
            sin_p[k] += 1
        else:
            pesos = round(c * p, 2)
            mo[k] += pesos               # la MO del libreto es la suma de sus filas tal como se mandan (al centavo)
        pts[k] += num_es(tot)
        if texto(tk):
            tks[k].add(texto(tk))
        prest[k].append(dict(n=n_pre[k], prestacion=cod_qp[:20], unidad=texto(uni)[:20], cantidad=c, puntos_unit=num_es(pu), puntos=num_es(tot),
                             precio=p, pesos=pesos, ticket=texto(tk)[:40]))
    mats = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, ""]))
    uni_ma = ma["Unidad de Medida"] if "Unidad de Medida" in ma.columns else [""] * len(ma)
    trab_ma = ma["Trabajo"] if "Trabajo" in ma.columns else [""] * len(ma)     # la orden de SAP del libreto
    trabajos = defaultdict(set)
    rep_mat = 0
    for lcl, lib, est, cod, pos, rec, tk, uni, tr in zip(col(ma, "Código LCL/LCA"), col(ma, "Código Libreto"), col(ma, "Estado Libreto"), col(ma, "Código Material"),
                                                         col(ma, "Cant. Posada"), col(ma, "Cant. Recuperada"), col(ma, "Ticket"), uni_ma, trab_ma):
        k = (lcl_norm(lcl), libreto_norm(lib), texto(est).upper())
        cod = re.sub(r"\s+", "", texto(cod))
        if not k[0] or not k[1] or not cod:
            continue
        t_ = re.sub(r"\D", "", texto(tr)).lstrip("0")
        if t_:
            trabajos[k].add(t_)
        rep_mat += 1 if cod in mats[k] else 0
        x = mats[k][cod]
        x[0] += num_es(pos)
        x[1] += num_es(rec)
        x[2] = x[2] or texto(uni)[:20]
        if texto(tk):
            tks[k].add(texto(tk))
    elegidos = {}
    rep_lib = 0
    for _, r in li.iterrows():
        k2 = (lcl_norm(r["lcl"]), libreto_norm(r["libreto"]))
        if not k2[0] or not k2[1]:
            continue
        previo = elegidos.get(k2)
        rep_lib += 1 if previo is not None else 0
        if previo is None or (texto(previo["estado_libreto"]).upper() in ("ANN", "RIF") and texto(r["estado_libreto"]).upper() not in ("ANN", "RIF")):
            elegidos[k2] = r                       # con el número repetido queda el libreto vivo
    libretos, vistos = [], set()
    for k2, r in elegidos.items():
        k = (k2[0], k2[1], texto(r["estado_libreto"]).upper())
        vistos.add(k)
        libretos.append(dict(lcl=k[0], libreto=k[1], descripcion=texto(r["descripcion"])[:300], estado_libreto=texto(r["estado_libreto"]).upper()[:12],
                             estado_lcl=texto(r["estado_lcl"]).upper()[:20], fecha_autocert=fecha_scm(r["fecha_autocert"]), mo_pesos=round(mo.get(k, 0.0), 2),
                             puntos=round(pts.get(k, 0.0), 2), n_prest=n_pre.get(k, 0), sin_precio=sin_p.get(k, 0), tickets=sorted(tks.get(k, set()))[:60],
                             materiales=[dict(material=c, posada=round(v[0], 3), recuperada=round(v[1], 3), unidad=v[2]) for c, v in sorted(mats.get(k, {}).items())],
                             prestaciones=prest.get(k, []), trabajos=sorted(trabajos.get(k, set()))[:60]))
    huerfanos = (set(mo) | set(mats)) - vistos
    libretos.sort(key=lambda l: (l["lcl"], l["libreto"]))
    # lo que queda registrado de cada archivo (filas, duplicados por llave natural y fechas raras)
    metas["libretos"].update(filas=len(li), duplicados=rep_lib, fuera_rango=fuera_de_rango(fecha_scm(v) for v in li["fecha_autocert"]))
    metas["prestaciones"].update(filas=len(pr), duplicados=0, fuera_rango=0)
    # varias filas del mismo material en un libreto (una por ticket) se suman: no son duplicados, se informan como agregadas
    metas["materiales"].update(filas=len(ma), duplicados=0, fuera_rango=0, agregadas=rep_mat)
    metas["precios"] = {"precios_archivo": os.path.basename(ruta_precios), "precios_sha": precios_sha,
                        "precios_modificado": datetime.fromtimestamp(os.path.getmtime(ruta_precios)).strftime("%Y-%m-%d %H:%M:%S")}
    return libretos, corte_de(f_lib), precios_sha, os.path.basename(f_lib), len(huerfanos), sum(sin_p.values()), metas


def armar_deuda(ruta: str):
    m = re.search(r"(\d{2})-(\d{2})-(20\d{2})", os.path.basename(ruta))
    corte = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else datetime.fromtimestamp(os.path.getmtime(ruta)).strftime("%Y-%m-%d")
    usar = ["LCL", "QR PMS", "ID PLANILLA SONNY", "PDL / TICKET", "NÚMERO LIBRETO DE MEDIDA", "VALOR MO", "VLR. MATERIAL", "ESTADO", "ESTADO.1",
            "RESPONSABILIDAD", "FECHA ESTADO / ENVIO"]
    de = pd.read_excel(ruta, sheet_name="Data", dtype=str)
    faltan = [c for c in usar if c not in de.columns]
    if faltan:
        print(f"AVISO: el informe de deuda no trae {faltan}; no se carga")
        return corte, []
    de = de[usar]                       # solo estas columnas: nada de nombres ni cédulas
    filas = []
    for i, r in de.iterrows():
        lcl, pdl, idp = lcl_norm(r["LCL"]), texto(r["PDL / TICKET"]), texto(r["ID PLANILLA SONNY"])
        if not lcl and not re.search(r"(CDOPP|DF20)", pdl.upper()) and not re.fullmatch(r"\d{5}", idp):
            continue
        filas.append(dict(item=str(i + 2), lcl=lcl, qr=texto(r["QR PMS"])[:100], id_planilla=idp[:40], pdl_ticket=pdl[:80],
                          libreto=texto(r["NÚMERO LIBRETO DE MEDIDA"])[:20], mo=num_es(r["VALOR MO"]), mat=num_es(r["VLR. MATERIAL"]),
                          estado=texto(r["ESTADO"])[:80], estado_desc=texto(r["ESTADO.1"])[:80], responsabilidad=texto(r["RESPONSABILIDAD"])[:40],
                          fecha_estado=(pd.to_datetime(texto(r["FECHA ESTADO / ENVIO"]), errors="coerce").strftime("%Y-%m-%d %H:%M:%S")
                                        if not pd.isna(pd.to_datetime(texto(r["FECHA ESTADO / ENVIO"]), errors="coerce")) else "")))
    return corte, filas


COLS_LCL = ["Código LCL/LCA", "Estado", "Fecha Envío LCL/LCA", "Inicio Validez", "Fecha de Fin Validez", "Importe", "Trabajo", "Obra", "Acumulativa", "TAM"]


def archivo_lcl(carpeta: str):
    """El lcl_export más nuevo de la carpeta (o de su subcarpeta «scm», como lo deja el prompt de SCM) y, si no hay, el más nuevo de Descargas."""
    xs = [x for d in (carpeta, os.path.join(carpeta, "scm")) if d and os.path.isdir(d) for x in glob.glob(os.path.join(d, "lcl_export*.xls*"))]
    if not xs:
        xs = glob.glob(os.path.join(DESCARGAS, "lcl_export*.xls*"))
    return sorted(xs, key=os.path.getmtime)[-1] if xs else None


def bool_scm(v):
    t = texto(v).lower()
    if t in ("1", "1.0", "true", "verdadero", "si", "sí", "s"):
        return True
    if t in ("0", "0.0", "false", "falso", "no", "n"):
        return False
    return None


def armar_lcl(ruta: str):
    """Las filas del exporte de LCL (una por LCL × Trabajo) SOLO con las columnas que se usan (usecols): sin personas ni descripción."""
    meta = huella(ruta)
    df = pd.read_excel(ruta, dtype=str, header=0, usecols=lambda c: str(c).strip() in COLS_LCL)
    df.columns = [str(c).strip() for c in df.columns]
    faltan = [c for c in COLS_LCL if c not in df.columns]
    if faltan:
        sys.exit(f"El exporte de LCL no trae {faltan} (¿cambió de forma?)")
    filas, vistos, dup, inval = [], set(), 0, 0
    for r in df.itertuples(index=False):
        d = dict(zip(df.columns, r))
        lcl, tr = lcl_norm(d["Código LCL/LCA"]), re.sub(r"\D", "", texto(d["Trabajo"]))
        if not lcl or not tr:
            inval += 1
            continue
        k = (lcl, tr.lstrip("0"))
        dup += 1 if k in vistos else 0
        vistos.add(k)
        ob = texto(d["Obra"])
        try:
            obra = int(float(ob)) if ob else None
        except ValueError:
            obra = None
        filas.append(dict(lcl=lcl, trabajo=tr[:24], estado=texto(d["Estado"]).upper()[:20], fecha_envio=fecha_scm(d["Fecha Envío LCL/LCA"]),
                          inicio_validez=fecha_scm(d["Inicio Validez"]), fin_validez=fecha_scm(d["Fecha de Fin Validez"]), importe=num_es(d["Importe"]),
                          obra=obra if obra is None or 0 <= obra <= 100000 else None, acumulativa=bool_scm(d["Acumulativa"]), tam=texto(d["TAM"])[:10]))
    filas.sort(key=lambda f: (f["lcl"], f["trabajo"]))
    # el fin de validez es futuro por naturaleza: lo que se cuenta como fecha rara es el envío
    meta.update(filas=len(df), duplicados=dup, fuera_rango=fuera_de_rango(f["fecha_envio"] for f in filas), invalidas=inval)
    return filas, meta


def enviar_lcl(base, cab, ruta: str, filas: list, meta: dict, recalcular: bool, tam: int = 2000):
    """Registra la carga (scm/lcl) y manda las filas en lotes con cada LCL completa; el último lote va con fin=true."""
    m = dict(meta)
    n, dup, fr, inval = m.pop("filas"), m.pop("duplicados"), m.pop("fuera_rango"), m.pop("invalidas")
    corte = corte_de(ruta)
    cid = abrir_carga(base, cab, "scm", "lcl", m, n, dup, fr, corte=corte,
                      detalle={"nota": "solo LCL, estado, fechas, Importe, Trabajo, Obra, Acumulativa y TAM (sin Nombre técnico EDE, Ejecutor ni descripción)",
                               "invalidas": inval})
    lotes, lote, actual = [], [], None
    for f in filas:
        if len(lote) >= tam and f["lcl"] != actual:
            lotes.append(lote)
            lote = []
        lote.append(f)
        actual = f["lcl"]
    if lote:
        lotes.append(lote)
    t = Counter()
    ult = {}
    for i, lo in enumerate(lotes, 1):
        fin = i == len(lotes)
        d = enviar(base + "/scm-lcl", cab, {"archivo": os.path.basename(ruta)[:200], "corte": corte, "carga_id": cid, "filas": lo, "fin": fin, "recalcular": recalcular})
        for k in ("nuevas", "actualizadas", "ya_estaban", "de_un_exporte_anterior", "cambian_estado", "no_vigentes", "invalidas", "lcls", "lcls_con_caso"):
            t[k] += d.get(k) or 0
        ult = d
        print(f"  lote {i}: {len(lo)} filas de {d.get('lcls')} LCL → nuevas {d.get('nuevas')} · actualizadas {d.get('actualizadas')} · ya estaban {d.get('ya_estaban')}"
              f" · cambian de estado {d.get('cambian_estado')} · dejan de estar vigentes {d.get('no_vigentes')} · LCL con caso {d.get('lcls_con_caso')}"
              + (f" · casos recalculados {d.get('casos_recalculados')}" + (" (en segundo plano)" if d.get("recalculo_en_fondo") else "") if fin else ""))
    res = {k: int(v) for k, v in t.items()} | {"casos_recalculados": ult.get("casos_recalculados"), "recalculo_en_fondo": ult.get("recalculo_en_fondo")}
    print(f"TOTAL LCL: {len(filas)} filas · {len({f['lcl'] for f in filas})} LCL · nuevas {t['nuevas']} · actualizadas {t['actualizadas']} · ya estaban {t['ya_estaban']}"
          f" · LCL con caso {t['lcls_con_caso']} de {t['lcls']} ({100 * t['lcls_con_caso'] / max(1, t['lcls']):.1f} %)")
    cerrar_carga(base, cab, cid, len(filas), res)


def lotes_por_lcl(libretos: list, tam: int):
    lote, actual = [], None
    for l in libretos:
        if len(lote) >= tam and l["lcl"] != actual:
            yield lote
            lote = []
        lote.append(l)
        actual = l["lcl"]
    if lote:
        yield lote


def enviar(url, cab, cuerpo):
    r = requests.post(url, headers=cab, json=cuerpo, timeout=900)
    if r.status_code >= 400:
        try:
            det = r.json().get("detail")
        except Exception:
            det = r.text[:200]
        sys.exit(f"{url.rsplit('/', 1)[-1]}: HTTP {r.status_code} {det}")
    return r.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta", nargs="?", default=None)
    ap.add_argument("--precios", default=None, help="precios_mdo_mat.json (por defecto la copia de sgo7-repo)")
    ap.add_argument("--lote", type=int, default=200)
    ap.add_argument("--simular", action="store_true")
    ap.add_argument("--sin-deuda", action="store_true")
    ap.add_argument("--sin-recalculo", action="store_true")
    ap.add_argument("--solo-lcl", action="store_true", help="solo el exporte de LCL (lcl_export)")
    ap.add_argument("--sin-lcl", action="store_true", help="no manda el exporte de LCL aunque lo encuentre")
    ap.add_argument("--lcl", default=None, help="el archivo lcl_export*.xls a cargar (por defecto el más nuevo de la carpeta o de Descargas)")
    ap.add_argument("--config", default=os.path.join(DIR, "config_gestion.json"))
    a = ap.parse_args()
    carpeta = a.carpeta or (os.path.dirname(a.lcl) if a.solo_lcl and a.lcl else carpeta_por_defecto())
    print(f"carpeta: {carpeta}")
    f_lcl = None if a.sin_lcl else (a.lcl or archivo_lcl(carpeta))
    lcl_filas, lcl_meta = ([], None)
    if f_lcl:
        lcl_filas, lcl_meta = armar_lcl(f_lcl)
        est = Counter()
        por = {}
        for f in lcl_filas:
            por.setdefault(f["lcl"], set()).add(f["estado"])
        for v in por.values():
            est[" / ".join(sorted(v))] += 1
        print(f"LCL: {os.path.basename(f_lcl)} · {len(lcl_filas)} filas · {len(por)} LCL · estado de la LCL {dict(est.most_common())} · Importe "
              f"${sum(f['importe'] for f in lcl_filas) / 1e6:,.1f} M · repetidas {lcl_meta['duplicados']} · inválidas {lcl_meta['invalidas']} · corte {corte_de(f_lcl)}")
    elif a.solo_lcl:
        sys.exit("No encontré «lcl_export*.xls» en la carpeta ni en Descargas (pásalo con --lcl)")
    else:
        print("LCL: no hay «lcl_export*.xls» en la carpeta ni en Descargas — sigo sin él")
    if a.solo_lcl:
        if a.simular:
            return
        with open(a.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        enviar_lcl(cfg["api_url"].rstrip("/"), {"X-Agente-Token": cfg["token"]}, f_lcl, lcl_filas, lcl_meta, not a.sin_recalculo)
        return
    ruta_precios = a.precios or next((p for p in PRECIOS if os.path.exists(p)), None)
    if not ruta_precios:
        sys.exit("No encontré precios_mdo_mat.json (pásalo con --precios)")
    libretos, corte, precios_sha, archivo, huerfanos, sin_precio, metas = armar(carpeta, ruta_precios)
    vivos = [l for l in libretos if l["estado_libreto"] not in ("ANN", "RIF")]
    print(f"libretos: {len(libretos)} de {len({l['lcl'] for l in libretos})} LCL · vivos {len(vivos)} · MO de los vivos ${sum(l['mo_pesos'] for l in vivos) / 1e6:,.1f} M"
          f" · prestaciones sin precio {sin_precio} · filas de prestaciones/materiales sin libreto en el informe {huerfanos} · corte {corte}")
    print("estados:", dict(Counter(l["estado_libreto"] for l in libretos).most_common()))
    f_deu = None if a.sin_deuda else el_mas_nuevo(carpeta, "*Informe deuda*.xls*", obligatorio=False)
    corte_d, deuda = armar_deuda(f_deu) if f_deu else (None, [])
    if f_deu:
        print(f"deuda: {os.path.basename(f_deu)} · corte {corte_d} · {len(deuda)} filas con código · MO ${sum(f['mo'] for f in deuda) / 1e6:,.1f} M")
    if a.simular:
        return
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    base = cfg["api_url"].rstrip("/")
    cab = {"X-Agente-Token": cfg["token"]}
    tot, enl = Counter(), Counter()
    cids = {}
    for parte in ("libretos", "prestaciones", "materiales"):        # registro de cada archivo
        m = dict(metas[parte])
        filas, dup, fr, agr = m.pop("filas"), m.pop("duplicados"), m.pop("fuera_rango"), m.pop("agregadas", 0)
        det = (dict(metas["precios"], sin_precio=sin_precio, huerfanos=huerfanos) if parte == "prestaciones" else
               ({"nota": "número de libreto repetido (ANN + vivo): queda el vivo"} if parte == "libretos" and dup else
                ({"filas_agregadas": agr, "nota": "el mismo material en varias filas de un libreto (una por ticket) se suma"} if parte == "materiales" else {})))
        cids[parte] = abrir_carga(base, cab, "scm", parte, m, filas, dup, fr, corte=corte_de(os.path.join(carpeta, m["archivo"])), detalle=det)
    for n, lote in enumerate(lotes_por_lcl(libretos, a.lote), 1):
        d = enviar(base + "/scm", cab, {"archivo": archivo, "corte": corte, "precios_sha": precios_sha, "libretos": lote, "recalcular": not a.sin_recalculo,
                                        "carga_libretos": cids["libretos"], "carga_prestaciones": cids["prestaciones"], "carga_materiales": cids["materiales"]})
        for k in ("nuevos", "actualizados", "ya_estaban", "materiales", "prestaciones", "lcls", "lcls_con_caso", "casos_recalculados", "invalidos"):
            tot[k] += d.get(k) or 0
        enl.update(d.get("enlace_vivos") or {})
        print(f"  lote {n}: {len(lote)} libretos → nuevos {d.get('nuevos')} · actualizados {d.get('actualizados')} · ya estaban {d.get('ya_estaban')}"
              f" · LCL con caso {d.get('lcls_con_caso')}/{d.get('lcls')} · enlace {d.get('enlace_vivos')} · casos recalculados {d.get('casos_recalculados')}"
              + (" · RECÁLCULO PENDIENTE" if d.get("recalculo_pendiente") else ""))
    n_viv = sum(enl.values())
    print(f"TOTAL SCM: nuevos {tot['nuevos']} · actualizados {tot['actualizados']} · ya estaban {tot['ya_estaban']} · materiales {tot['materiales']}"
          f" · LCL con caso {tot['lcls_con_caso']} de {tot['lcls']} ({100 * tot['lcls_con_caso'] / max(1, tot['lcls']):.1f} %)"
          f" · libretos vivos con planilla {n_viv - enl.get('sin planilla', 0) - enl.get('ambiguo', 0)} de {n_viv}"
          f" ({100 * (n_viv - enl.get('sin planilla', 0) - enl.get('ambiguo', 0)) / max(1, n_viv):.1f} %) por regla {dict(enl)}")
    res = {k: int(v) for k, v in tot.items()} | {"enlace": dict(enl)}
    cerrar_carga(base, cab, cids["prestaciones"], tot["prestaciones"], {"prestaciones": int(tot["prestaciones"])})
    cerrar_carga(base, cab, cids["materiales"], tot["materiales"], {"materiales": int(tot["materiales"])})
    cerrar_carga(base, cab, cids["libretos"], len(libretos), res)
    if f_lcl:                       # después de los libretos (el servidor mira si cada LCL tiene libreto)
        enviar_lcl(base, cab, f_lcl, lcl_filas, lcl_meta, not a.sin_recalculo)
    if deuda:
        t = Counter()
        md = huella(f_deu)
        cid_d = abrir_carga(base, cab, "deuda", "informe", md, len(deuda), 0, fuera_de_rango(f["fecha_estado"] for f in deuda), corte=f"{corte_d} 00:00:00",
                            detalle={"nota": "solo códigos, montos y estado (sin nombres ni cédulas)"})
        for i in range(0, len(deuda), 2000):
            d = enviar(base + "/deuda", cab, {"archivo": os.path.basename(f_deu)[:200], "corte": corte_d, "filas": deuda[i:i + 2000], "recalcular": not a.sin_recalculo,
                                              "carga_id": cid_d})
            for k in ("nuevas", "actualizadas", "ya_estaban", "con_lcl", "casos_recalculados"):
                t[k] += d.get(k) or 0
        print(f"TOTAL deuda: nuevas {t['nuevas']} · actualizadas {t['actualizadas']} · ya estaban {t['ya_estaban']} · con LCL {t['con_lcl']} · recálculos {t['casos_recalculados']}")
        cerrar_carga(base, cab, cid_d, len(deuda), {k: int(v) for k, v in t.items()})


if __name__ == "__main__":
    main()
