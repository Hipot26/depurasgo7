# -*- coding: utf-8 -*-
"""Carga el Historial y la Lista de Planillas de Sonny en Gestión General de SGO7 (2026-10-23). Solo lectura de los Excel.

    python -I cargar_sonny_gestion.py                       → los dos exportes de la carpeta semanal más nueva
                                                              (Descargas\\exportes-sistemas\\AAAA-MM-DD\\) o, si no hay, de Descargas
    python -I cargar_sonny_gestion.py <carpeta>             → los de esa carpeta
    python -I cargar_sonny_gestion.py --simular             → arma las filas y cuenta, sin enviar nada
    python -I cargar_sonny_gestion.py --historial H.xlsx --lista L.xlsx

Se exportan a mano dos consultas de Sonny:
  · «Historial de Planillas.xlsx»: un cambio de estado por fila (Id, Id_planilla, Estado, Fecha, Observaciones…).
  · «Lista de Planillas.xlsx»: una fila por planilla (Id, Lcl, Ticket, Pdl, Odm, Nodo, Nombre_supervisor…).
Cada cambio del historial se liga a su planilla de la lista y sale con {id_cambio, id_planilla, codigo, ticket, nodo,
supervisor, estado, fecha, observacion}. codigo = la LCL (63…) o, si la planilla no trae LCL, su ticket; ticket = el DF
(en las de mayor impacto Sonny trae los dos y en SGS el proyecto es el ticket). Se envían en lotes de 500 a
POST {api_url}/sonny con la cabecera X-Agente-Token de config_gestion.json (el token nunca se imprime). Reenviar el mismo
archivo no duplica: el servidor hace upsert por id_cambio (la columna Id del historial).

además sube cada planilla de la Lista (POST {api_url}/sonny-planillas): Lcl, Ticket, Pdl, QR (= request_code de PMS), Odm,
Nodo, supervisor, estado, MO y MAT plan y real, el campo Libreto (solo se muestra: NO es llave) y las fechas y estados de
supervisor e ingeniero (pasos 19 y 25-26). Cédulas, correos y nombres de cuadrilla, ingeniero o Enel NO se mandan.

Los Excel de Descargas son datos no confiables: se leen con pandas (sin macros) y este script vive fuera de Descargas.
Desde 2026-10-23: cada Excel queda registrado en el servidor como una carga (POST {api_url}/carga: nombre, tamaño, sha256, fecha del
archivo, filas, duplicados y fechas raras, usuario y equipo del PC) y las filas llevan su carga_id.
Desde 2026-10-25: si en la carpeta están, sube también (POST {api_url}/sonny-nodos):
  · «Lista Cruzada de Nodos.xlsx» (Reportes > Consultas, Contrato JA10185664): un nodo por fila, con su estado, la fecha de ese estado y su
    planilla (vacío = sin planilla). Lider_cuadrilla (correo) y Nombre_corto (persona) NO se leen.
  · «Nodos por gestion almacen.xlsx» (2026-01-01 → hoy): el material de cada nodo que espera planilla (Confirmado / Pendiente / Sin material),
    unido al nodo por Id_nodo. Capturo (persona) NO se lee.
  · «Historial de Actividades.xlsx» (2026-01-01 → hoy): del texto de cada acción solo se saca el tipo y la fecha por planilla (creó, cambió el
    estado, la asignó a un firmante —de Enel, Dominion o interventoría, sin el correo—, la editó) y la fecha en que almacén confirmó el
    material de un nodo. Usuario NO se lee; los nombres y correos del texto no salen del PC.
  · «Lista de Estados.xlsx»: solo se registra la carga (los 4 estados propios del contrato).
    python -I cargar_sonny_gestion.py <carpeta> --solo-nodos   → solo esos (sin volver a subir el Historial y la Lista de Planillas)
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
from datetime import datetime, timedelta

import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIR = os.path.dirname(os.path.abspath(__file__))
DESCARGAS = os.path.join(os.path.expanduser("~"), "Downloads")
SEMANAL = os.path.join(DESCARGAS, "exportes-sistemas")
RX_LCL = re.compile(r"(?<!\d)(63\d{8})(?!\d)")
RX_DF = re.compile(r"(?<![A-Za-z0-9])(DF20\d{10})(?!\d)", re.I)


def texto(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


def primero(rx, *valores):
    for v in valores:
        m = rx.search(texto(v))
        if m:
            return m.group(1).upper()
    return None


def fecha_txt(v) -> str:
    d = pd.to_datetime(texto(v), errors="coerce")
    return "" if pd.isna(d) else d.strftime("%Y-%m-%d %H:%M:%S")


def num(v) -> float:
    try:
        return round(float(texto(v) or 0), 2)
    except ValueError:
        return 0.0


def huella(ruta: str) -> dict:
    """Sha256, tamaño y fecha de modificación (hora del PC = Colombia) de un archivo exportado."""
    h = hashlib.sha256()
    with open(ruta, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    st = os.stat(ruta)
    return {"_ruta": ruta, "archivo": os.path.basename(ruta)[:300], "sha256": h.hexdigest(), "tamano": st.st_size,       # _ruta = lo que se sube
            "modificado": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")}


def planillas_de(ruta_l: str) -> list:
    """Una fila por planilla de la Lista, sin cédulas ni correos."""
    lp = pd.read_excel(ruta_l, dtype=str)
    out = []
    for _, x in lp.iterrows():
        idp = texto(x.get("Id"))
        if not idp:
            continue
        out.append(dict(id_planilla=idp[:40], lcl=primero(RX_LCL, x.get("Lcl"), x.get("Odm")),
                        ticket=primero(RX_DF, x.get("Ticket"), x.get("Lcl"), x.get("Pdl"), x.get("Odm")),
                        pdl=texto(x.get("Pdl"))[:80], qr=texto(x.get("QR"))[:100], odm=texto(x.get("Odm"))[:200], nodo=texto(x.get("Nodo"))[:200],
                        supervisor=re.sub(r"\s+", " ", texto(x.get("Nombre_supervisor")))[:150], estado=re.sub(r"\s+", " ", texto(x.get("Estado")))[:60],
                        libreto_sonny=texto(x.get("Libreto"))[:20], mo_plan=num(x.get("Valor_mo_plan")), mat_plan=num(x.get("Valor_mat_plan")),
                        mo_real=num(x.get("Valor_mo_real")), mat_real=num(x.get("Valor_mat_real")), fecha_planilla=fecha_txt(x.get("Fecha_planilla")),
                        fecha_supervisor=fecha_txt(x.get("Fecha_supervisor")), fecha_ingeniero=fecha_txt(x.get("Fecha_ingeniero")),
                        fecha_enel=fecha_txt(x.get("Fecha_enel")), estado_supervisor=texto(x.get("Estado_supervisor"))[:30],
                        estado_ingeniero=texto(x.get("Estado_ingeniero"))[:30]))
    return out


def armar(ruta_h: str, ruta_l: str):
    h = pd.read_excel(ruta_h, dtype=str)
    lp = pd.read_excel(ruta_l, dtype=str)
    for col in ("Id", "Id_planilla", "Estado", "Fecha"):
        if col not in h.columns:
            sys.exit(f"El historial no trae la columna «{col}» (columnas: {list(h.columns)})")
    for col in ("Id", "Lcl", "Ticket", "Nodo"):
        if col not in lp.columns:
            sys.exit(f"La lista no trae la columna «{col}» (columnas: {list(lp.columns)})")
    print(f"historial: {len(h)} cambios de {h['Id_planilla'].nunique()} planillas · {h['Fecha'].min()} → {h['Fecha'].max()}")
    print(f"lista: {len(lp)} planillas")
    id_unico = h["Id"].notna().all() and h["Id"].is_unique
    if not id_unico:
        print("AVISO: la columna Id del historial no es única; la llave pasa a sha(id_planilla|fecha|estado)")
    lista = {texto(r["Id"]): r for _, r in lp.iterrows()}
    filas, sin_lista = [], 0
    for _, r in h.iterrows():
        pl = texto(r["Id_planilla"])
        est = re.sub(r"\s+", " ", texto(r["Estado"]))
        f = fecha_txt(r["Fecha"])
        if not pl or not est or not f:
            continue
        idc = texto(r["Id"]) if id_unico else hashlib.sha256(f"{pl}|{f}|{est}".encode()).hexdigest()[:40]
        x = lista.get(pl)
        if x is None:
            sin_lista += 1
            lcl = df = None
            nodo = sup = ""
        else:
            lcl = primero(RX_LCL, x.get("Lcl"), x.get("Odm"))
            df = primero(RX_DF, x.get("Ticket"), x.get("Lcl"), x.get("Pdl"), x.get("Odm"))
            nodo = texto(x.get("Nodo"))[:200]
            sup = re.sub(r"\s+", " ", texto(x.get("Nombre_supervisor")))[:150]
        filas.append(dict(id_cambio=idc, id_planilla=pl, codigo=lcl or df, ticket=df, nodo=nodo, supervisor=sup, estado=est[:60], fecha=f,
                          observacion=texto(r.get("Observaciones"))[:2000]))
    filas.sort(key=lambda f: (f["id_planilla"], f["fecha"], f["id_cambio"]))     # la historia de una planilla queda en un mismo lote
    if sin_lista:
        print(f"AVISO: {sin_lista} cambios de planillas que no están en la Lista (van sin código)")
    return filas


def rutas(a):
    """Historial y Lista: los dados, o los de la carpeta (la semanal más nueva o Descargas)."""
    carpeta = a.carpeta
    if not carpeta:
        semanas = sorted(d for d in glob.glob(os.path.join(SEMANAL, "20??-??-??")) if glob.glob(os.path.join(d, "Lista de Planillas*.xls*")))
        carpeta = semanas[-1] if semanas else DESCARGAS

    def buscar(nombre):
        exacto = os.path.join(carpeta, f"{nombre}.xlsx")
        if os.path.exists(exacto):
            return exacto
        xs = sorted(glob.glob(os.path.join(carpeta, f"{nombre}*.xls*")), key=os.path.getmtime)
        if not xs:
            sys.exit(f"No encontré «{nombre}» en {carpeta}")
        return xs[-1]
    return a.historial or buscar("Historial de Planillas"), a.lista or buscar("Lista de Planillas")


def carpeta_de(a) -> str:
    """La carpeta dada, la semanal más nueva con «Lista de Planillas» o Descargas."""
    if a.carpeta:
        return a.carpeta
    semanas = sorted(d for d in glob.glob(os.path.join(SEMANAL, "20??-??-??")) if glob.glob(os.path.join(d, "Lista de Planillas*.xls*")))
    return semanas[-1] if semanas else DESCARGAS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta", nargs="?", default=None, help="carpeta con «Historial de Planillas*.xlsx» y «Lista de Planillas*.xlsx»")
    ap.add_argument("--historial", default=None)
    ap.add_argument("--lista", default=None)
    ap.add_argument("--lote", type=int, default=500)
    ap.add_argument("--simular", action="store_true", help="arma y cuenta las filas, sin enviar")
    ap.add_argument("--sin-recalculo", action="store_true", help="la Lista se sube sin recalcular los casos en cada lote")
    ap.add_argument("--config", default=os.path.join(DIR, "config_gestion.json"))
    ap.add_argument("--solo-nodos", action="store_true", help="solo Lista Cruzada, gestión almacén, Actividades y Estados")
    ap.add_argument("--cruzada", default=None)
    ap.add_argument("--almacen", default=None)
    ap.add_argument("--actividades", default=None)
    ap.add_argument("--estados", default=None)
    a = ap.parse_args()
    if not a.solo_nodos:
        cargar_planillas(a)
    cargar_nodos(a, carpeta_de(a))


def cargar_planillas(a):
    a.historial, a.lista = rutas(a)
    print(f"historial: {a.historial}\nlista: {a.lista}")

    filas = armar(a.historial, a.lista)
    pls = {}
    for f in filas:
        pls.setdefault(f["id_planilla"], f)
    print(f"filas: {len(filas)} · planillas: {len(pls)} · con LCL {sum(1 for f in pls.values() if (f['codigo'] or '').startswith('63'))}"
          f" · con ticket {sum(1 for f in pls.values() if f['ticket'])} · sin código {sum(1 for f in pls.values() if not f['codigo'])}")
    print("estados:", dict(Counter(f["estado"] for f in filas).most_common()))
    pls_lista = planillas_de(a.lista)
    print(f"lista: {len(pls_lista)} planillas · con LCL {sum(1 for p in pls_lista if p['lcl'])} · con ticket {sum(1 for p in pls_lista if p['ticket'])}"
          f" · con QR {sum(1 for p in pls_lista if p['qr'])} · MO real ${sum(p['mo_real'] for p in pls_lista) / 1e6:,.1f} M")
    if a.simular:
        return

    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    url = cfg["api_url"].rstrip("/") + "/sonny"
    cab = {"X-Agente-Token": cfg["token"]}
    tot = Counter()
    desconocidos = Counter()
    base = cfg["api_url"].rstrip("/")
    ids_h = [f["id_cambio"] for f in filas]
    ids_l = [p["id_planilla"] for p in pls_lista]
    cid_h = abrir_carga(base, cab, "sonny", "historial", huella(a.historial), len(filas), len(ids_h) - len(set(ids_h)), fuera_de_rango(f["fecha"] for f in filas))
    cid_l = abrir_carga(base, cab, "sonny", "lista", huella(a.lista), len(pls_lista), len(ids_l) - len(set(ids_l)),
                        fuera_de_rango(p["fecha_planilla"] for p in pls_lista), detalle={"nota": "sin cédulas ni correos; el campo Libreto no es llave"})
    for i in range(0, len(filas), a.lote):
        lote = filas[i:i + a.lote]
        r = requests.post(url, headers=cab, json={"archivo": os.path.basename(a.historial), "filas": lote, "carga_id": cid_h}, timeout=900)
        if r.status_code >= 400:
            try:
                det = r.json().get("detail")
            except Exception:
                det = r.text[:200]
            sys.exit(f"lote {i // a.lote + 1}: HTTP {r.status_code} {det}")
        d = r.json()
        for k in ("nuevas", "actualizadas", "ya_estaban", "invalidas", "casos_recalculados"):
            tot[k] += d.get(k) or 0
        desconocidos.update(d.get("estados_desconocidos") or {})
        print(f"  lote {i // a.lote + 1}: {len(lote)} filas → nuevas {d.get('nuevas')} · actualizadas {d.get('actualizadas')} · ya estaban {d.get('ya_estaban')}"
              f" · planillas con caso {d.get('planillas_con_caso')} (LCL {d.get('por_lcl')}, ticket {d.get('por_ticket')}) · sin caso {d.get('planillas_sin_caso')}"
              f" · sin código {d.get('planillas_sin_codigo')} · casos recalculados {d.get('casos_recalculados')}"
              + (" · RECÁLCULO PENDIENTE (Centro MO no respondió)" if d.get("recalculo_pendiente") else ""))
    print(f"TOTAL: nuevas {tot['nuevas']} · actualizadas {tot['actualizadas']} · ya estaban {tot['ya_estaban']} · inválidas {tot['invalidas']}"
          f" · recálculos de casos {tot['casos_recalculados']}" + (f" · estados desconocidos {dict(desconocidos)}" if desconocidos else ""))
    cerrar_carga(base, cab, cid_h, len(filas), {k: int(v) for k, v in tot.items()})

    # la Lista de Planillas (MO/MAT, QR y fechas de supervisor e ingeniero), en lotes de 500
    url_l = cfg["api_url"].rstrip("/") + "/sonny-planillas"
    tl = Counter()
    for i in range(0, len(pls_lista), a.lote):
        lote = pls_lista[i:i + a.lote]
        r = requests.post(url_l, headers=cab, json={"archivo": os.path.basename(a.lista), "planillas": lote, "recalcular": not a.sin_recalculo,
                                                    "carga_id": cid_l}, timeout=900)
        if r.status_code >= 400:
            try:
                det = r.json().get("detail")
            except Exception:
                det = r.text[:200]
            sys.exit(f"lista, lote {i // a.lote + 1}: HTTP {r.status_code} {det}")
        d = r.json()
        for k in ("nuevas", "actualizadas", "ya_estaban", "con_caso", "sin_codigo", "casos_recalculados"):
            tl[k] += d.get(k) or 0
        print(f"  lista, lote {i // a.lote + 1}: {len(lote)} planillas → nuevas {d.get('nuevas')} · actualizadas {d.get('actualizadas')} · ya estaban {d.get('ya_estaban')}"
              f" · con caso {d.get('con_caso')} · casos recalculados {d.get('casos_recalculados')}" + (" · RECÁLCULO PENDIENTE" if d.get("recalculo_pendiente") else ""))
    print(f"TOTAL lista: nuevas {tl['nuevas']} · actualizadas {tl['actualizadas']} · ya estaban {tl['ya_estaban']} · con caso {tl['con_caso']} de {len(pls_lista)}"
          f" · sin código {tl['sin_codigo']} · recálculos {tl['casos_recalculados']}")
    cerrar_carga(base, cab, cid_l, len(pls_lista), {k: int(v) for k, v in tl.items()})


# ── Sonny por etapa: Lista Cruzada de Nodos, Nodos por gestion almacen, Historial de Actividades, Lista de Estados ─────────────────
# El texto del Historial de Actividades trae nombres y correos («Asignó la planilla N al <persona> <correo>»): de cada acción solo se saca
# el tipo, el número de la planilla (o del nodo, o de la gestión de almacén) y la fecha. Lo demás no sale del PC.
RX74_SINC = re.compile(r"(\S+?)\(Lcl:([^)]+)\)")
RX74_ASIGNA = re.compile(r"^Asignó el nodo (.+) \(([^()]*)\) a \S+")
RX74_FINALIZA = re.compile(r"^Finalizó el Nodo (.+?) \(Lcl:([^)]+)\)")
RX74_CREA = re.compile(r"^Creó la planilla (\d+)")
RX74_ESTADO = re.compile(r"^Cambio el estado de la planilla (\d+) a (.+?)\.?\s*$")
RX74_FIRMA = re.compile(r"^Asignó la planilla (\d+) al .*@([\w.\-]+?)\.?\s*$")
RX74_EDITA = re.compile(r"^(?:Agregó en la planilla|Eliminó en la planilla|Cambio en la planilla|Actualizó todos los items de materiales de la planilla|"
                        r"Actualizó los datos complementarios de la planilla|Cargó el plano en la planilla)\s+(\d+)")
RX74_CONFIRMA = re.compile(r"^Confirmo el (consumo|reintegro) del Id (\d+)")
ROL_DOMINIO = {"enel.com": "enel", "cidet.org.co": "interventoria", "dominion-global.com": "dominion"}
PERSONAS_74 = {"Lider_cuadrilla", "Nombre_corto", "Capturo", "Usuario"}         # columnas con personas: no se leen


def opcional(carpeta: str, nombre: str, dado=None):
    if dado:
        return dado
    exacto = os.path.join(carpeta, f"{nombre}.xlsx")
    if os.path.exists(exacto):
        return exacto
    xs = sorted(glob.glob(os.path.join(carpeta, f"{nombre}*.xls*")), key=os.path.getmtime)
    return xs[-1] if xs else None


def leer_sin_personas(ruta: str) -> pd.DataFrame:
    return pd.read_excel(ruta, dtype=str, usecols=lambda c: str(c).strip() not in PERSONAS_74)


def actividad_de(ruta: str):
    """Historial de Actividades → (actividad por planilla, confirmación de almacén por Id de gestión, conteo de acciones)."""
    act = leer_sin_personas(ruta)
    for col in ("Fecha", "Log"):
        if col not in act.columns:
            sys.exit(f"El Historial de Actividades no trae la columna «{col}» (columnas: {list(act.columns)})")
    pl: dict = {}
    conf: dict = {}
    cuenta = Counter()
    for f_, lg in zip(act["Fecha"], act["Log"]):
        f = fecha_txt(f_)
        lg = texto(lg)
        if not f or not lg:
            continue
        m = RX74_CREA.match(lg)
        if m:
            pl.setdefault(m.group(1), []).append((f, "crea", ""))
            cuenta["crea"] += 1
            continue
        m = RX74_ESTADO.match(lg)
        if m:
            pl.setdefault(m.group(1), []).append((f, "estado", re.sub(r"\s+", " ", m.group(2)).strip()[:60]))
            cuenta["estado"] += 1
            continue
        m = RX74_FIRMA.match(lg)
        if m:
            pl.setdefault(m.group(1), []).append((f, "firma", ROL_DOMINIO.get(m.group(2).lower().rstrip("."), "otro")))
            cuenta["firma"] += 1
            continue
        m = RX74_EDITA.match(lg)
        if m:
            pl.setdefault(m.group(1), []).append((f, "edita", ""))
            cuenta["edita"] += 1
            continue
        m = RX74_CONFIRMA.match(lg)
        if m:
            conf[m.group(2)] = max(conf.get(m.group(2), ""), f)
            cuenta["confirma"] += 1
            continue
        if RX74_SINC.search(lg) or RX74_ASIGNA.match(lg) or RX74_FINALIZA.match(lg) or lg.startswith(("Liberó el nodo", "Asoció el nodo", "Desasoció el nodo")):
            cuenta["nodo"] += 1
        else:
            cuenta["otra"] += 1
    filas = []
    for idp, evs in pl.items():
        evs.sort()
        est = [e for e in evs if e[1] == "estado"]
        f_est, estado = (est[-1][0], est[-1][2]) if est else ("", "")
        firmas = [e for e in evs if e[1] == "firma"]
        tras = [e for e in firmas if not f_est or e[0] >= f_est]
        cre = [e[0] for e in evs if e[1] == "crea"]
        ult = evs[-1]
        filas.append(dict(id_planilla=idp[:40], f_creada=min(cre) if cre else "", estado_act=estado, f_estado_act=f_est, f_firma=tras[0][0] if tras else "",
                          n_firmas=len(firmas), roles=dict(Counter(e[2] for e in firmas)), f_ultima=ult[0], que_ultima=ult[1], n_act=len(evs)))
    return filas, conf, cuenta, len(act)


def nodos_de(ruta_cruz: str, ruta_alm, conf: dict):
    """Lista Cruzada de Nodos (+ Nodos por gestion almacen por Id_nodo) → un nodo por fila, sin personas."""
    x = leer_sin_personas(ruta_cruz)
    for col in ("Id_nodo", "Lcl", "Nodo", "Estado_nodo", "Id_planilla", "Fecha_estado"):
        if col not in x.columns:
            sys.exit(f"La Lista Cruzada no trae la columna «{col}» (columnas: {list(x.columns)})")
    alm: dict = {}
    n_alm = 0
    if ruta_alm:
        g = leer_sin_personas(ruta_alm)
        n_alm = len(g)
        for _, r in g.iterrows():
            k = texto(r.get("Id_nodo"))
            if not k:
                continue
            fa = fecha_txt(r.get("Fecha_asignacion"))
            a = alm.setdefault(k, {"n": 0, "f": "", "ids": [], "lcl": texto(r.get("Lcl")), "nodo": texto(r.get("Nodo")), "odm": texto(r.get("Odm")), "pdl": texto(r.get("Pdl"))})
            a["n"] += 1
            a["ids"].append(texto(r.get("Id")))
            if fa >= a["f"]:
                a.update(f=fa, estado=texto(r.get("Estado_material")), mov=texto(r.get("Movimiento")), id=texto(r.get("Id")))
    out, vistos = [], set()
    for _, r in x.iterrows():
        k = texto(r.get("Id_nodo"))
        if not k or k in vistos:
            continue
        vistos.add(k)
        a = alm.get(k) or {}
        ids = sorted({re.sub(r"\D", "", t) for t in texto(r.get("Id_planilla")).split(",") if re.sub(r"\D", "", t)})
        fc = max((conf.get(i, "") for i in a.get("ids") or []), default="")
        out.append(dict(id_nodo=k[:20], codigo=texto(r.get("Lcl"))[:40], odm=texto(r.get("Odm"))[:200], pdl=texto(r.get("Pdl"))[:300], qr=texto(r.get("QR"))[:100],
                        nodo=texto(r.get("Nodo"))[:120], estado_nodo=texto(r.get("Estado_nodo"))[:20], id_planillas=ids[:20],
                        estado_planilla=texto(r.get("Estado_planilla"))[:200], f_creacion=fecha_txt(r.get("Fecha_creacion")), f_estado=fecha_txt(r.get("Fecha_estado")),
                        estado_material=(a.get("estado") or "")[:20], movimiento=(a.get("mov") or "")[:20], f_almacen=a.get("f") or "", n_almacen=int(a.get("n") or 0),
                        id_gestion=(a.get("id") or "")[:20], f_confirmado=fc))
    # nodos de gestión almacén que la Lista Cruzada no trae (más nuevos que ella): entran con lo que se sabe, sin estado de nodo
    for k, a in alm.items():
        if k in vistos:
            continue
        out.append(dict(id_nodo=k[:20], codigo=a["lcl"][:40], odm=a["odm"][:200], pdl=a["pdl"][:300], qr="", nodo=a["nodo"][:120], estado_nodo="", id_planillas=[],
                        estado_planilla="", f_creacion="", f_estado="", estado_material=(a.get("estado") or "")[:20], movimiento=(a.get("mov") or "")[:20],
                        f_almacen=a.get("f") or "", n_almacen=int(a["n"]), id_gestion=(a.get("id") or "")[:20],
                        f_confirmado=max((conf.get(i, "") for i in a["ids"]), default="")))
    return out, len(x), n_alm


def cargar_nodos(a, carpeta: str):
    """Lista Cruzada + gestión almacén + Historial de Actividades + Lista de Estados (si están en la carpeta)."""
    r_cruz = opcional(carpeta, "Lista Cruzada de Nodos", a.cruzada)
    r_alm = opcional(carpeta, "Nodos por gestion almacen", a.almacen)
    r_act = opcional(carpeta, "Historial de Actividades", a.actividades)
    r_est = opcional(carpeta, "Lista de Estados", a.estados)
    print(f"\nSonny por etapa (carpeta {carpeta}):\n   lista cruzada: {r_cruz}\n   gestión almacén: {r_alm}\n   actividades: {r_act}\n   estados: {r_est}")
    if not (r_cruz or r_act):
        print("   no están la Lista Cruzada de Nodos ni el Historial de Actividades: nada que subir de Sonny por etapa")
        return
    conf, acts, cuenta, n_act = {}, [], Counter(), 0
    if r_act:
        acts, conf, cuenta, n_act = actividad_de(r_act)
        print(f"   actividades: {n_act} acciones → {len(acts)} planillas con actividad · {dict(cuenta)} · confirmaciones de almacén {len(conf)}")
    nodos, n_cruz, n_alm = ([], 0, 0)
    if r_cruz:
        nodos, n_cruz, n_alm = nodos_de(r_cruz, r_alm, conf)
        sinpl = Counter(n["estado_nodo"] or "(sin estado)" for n in nodos if not n["id_planillas"])
        print(f"   lista cruzada: {n_cruz} filas → {len(nodos)} nodos · sin planilla {sum(sinpl.values())} {dict(sinpl)} · gestión almacén {n_alm} filas"
              f" · material {dict(Counter(n['estado_material'] for n in nodos if n['estado_material']))}")
    elif r_alm:
        print("   AVISO: sin la Lista Cruzada no se suben los nodos (la gestión de almacén se une a ella por Id_nodo)")
    estados = []
    if r_est:
        e = leer_sin_personas(r_est)
        estados = [texto(v) for v in e.get("Estado", [])][:20]
        print(f"   estados del contrato: {estados}")
    if a.simular:
        return
    with open(a.config, encoding="utf-8") as fh:
        cfg = json.load(fh)
    base = cfg["api_url"].rstrip("/")
    cab = {"X-Agente-Token": cfg["token"]}
    url = base + "/sonny-nodos"

    def enviar(cid, clave, filas, ultimo_fin=False):
        tot = Counter()
        for i in range(0, len(filas), a.lote):
            lote = filas[i:i + a.lote]
            fin = ultimo_fin and i + a.lote >= len(filas)
            r = requests.post(url, headers=cab, timeout=900, json={"archivo": "", "carga_id": cid, clave: lote, "fin": fin, "recalcular": not a.sin_recalculo})
            if r.status_code >= 400:
                try:
                    det = r.json().get("detail")
                except Exception:
                    det = r.text[:200]
                sys.exit(f"{clave}, lote {i // a.lote + 1}: HTTP {r.status_code} {det}")
            d = r.json()
            for k in ("nuevas", "actualizadas", "ya_estaban", "invalidas", "no_vigentes", "con_caso", "sin_planilla", "act_nuevas", "act_actualizadas",
                      "act_ya_estaban", "casos_recalculados"):
                tot[k] += d.get(k) or 0
            print(f"   {clave}, lote {i // a.lote + 1}: {len(lote)} → " + " · ".join(f"{k} {d.get(k)}" for k in (
                "nuevas", "actualizadas", "ya_estaban", "invalidas", "con_caso", "no_vigentes", "act_nuevas", "act_actualizadas", "act_ya_estaban", "casos_recalculados")
                if d.get(k)) + (" · recálculo en segundo plano" if d.get("recalculo_en_fondo") else ""))
        return tot
    if r_alm:
        g_meta = huella(r_alm)
        cid_a = abrir_carga(base, cab, "sonny", "almacen", g_meta, n_alm, detalle={"nota": "se une a la Lista Cruzada por Id_nodo; sin la columna Capturo"})
        cerrar_carga(base, cab, cid_a, n_alm, {"unidos_por_id_nodo": sum(1 for n in nodos if n["n_almacen"])})
    if nodos:
        ids = [n["id_nodo"] for n in nodos]
        cid_n = abrir_carga(base, cab, "sonny", "nodos", huella(r_cruz), len(nodos), len(ids) - len(set(ids)), fuera_de_rango(n["f_estado"] for n in nodos),
                            detalle={"contrato": "JA10185664", "nota": "sin Lider_cuadrilla ni Nombre_corto"})
        tn = enviar(cid_n, "nodos", nodos, ultimo_fin=True)
        print("TOTAL nodos: " + " · ".join(f"{k} {v}" for k, v in tn.items() if v))
        cerrar_carga(base, cab, cid_n, len(nodos), {k: int(v) for k, v in tn.items()})
    if acts:
        cid_t = abrir_carga(base, cab, "sonny", "actividades", huella(r_act), n_act, fuera_rango=fuera_de_rango(x["f_ultima"] for x in acts),
                            detalle={"acciones": {k: int(v) for k, v in cuenta.items()}, "planillas": len(acts), "nota": "sin Usuario; del texto solo tipo, número y fecha"})
        tt = enviar(cid_t, "planillas", acts)
        print("TOTAL actividad: " + " · ".join(f"{k} {v}" for k, v in tt.items() if v))
        cerrar_carga(base, cab, cid_t, len(acts), {k: int(v) for k, v in tt.items()})
    if r_est:
        cid_e = abrir_carga(base, cab, "sonny", "estados", huella(r_est), len(estados), detalle={"estados": estados})
        cerrar_carga(base, cab, cid_e, 0, {"estados": len(estados)})


def abrir_carga(base, cab, sistema, parte, meta, filas, duplicados=0, fuera_rango=0, corte="", detalle=None):
    """Registra la carga en el servidor (nombre, tamaño, sha256, fecha del archivo, filas, usuario y equipo del PC) y devuelve su
    carga_id (None si el servidor es anterior a 2026-10-23)."""
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


def fuera_de_rango(fechas) -> int:
    """Fechas antes de 2020 o más de 2 días en el futuro (las mismas reglas que el control del servidor)."""
    tope = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    return sum(1 for f in fechas if f and (f < "2020-01-01" or f > tope))


if __name__ == "__main__":
    main()
